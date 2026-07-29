"""Containment policy for ``codeprobe run`` (codeprobe-f7rl.3).

A real run launches an autonomous agent with
``--dangerously-skip-permissions`` and executes mined third-party
test/verifier scripts. This module is the single decision point for where
that is allowed to happen:

- ``sandboxed`` — a real container was detected, or the user set
  ``CODEPROBE_SANDBOX=1`` themselves to declare the environment contained
  (see :func:`codeprobe.core.sandbox.is_sandboxed`). codeprobe never sets
  that variable itself.
- ``host-consented`` — no containment detected; the user passed
  ``--uncontained`` and accepted the disclosure.
- ``container`` — no containment detected and no consent flag, but a
  container engine is on PATH and the agent image is built
  (codeprobe-f7rl.5): the agent subprocess and mined scripts execute
  inside containers, so customers with Docker get containment by default
  instead of a consent prompt. Downstream code consults the decision via
  :func:`active_plan`.

Anything else is a hard refusal (``UNCONTAINED_REFUSED``). Pure policy
enforcement, no model calls (ZFC-allowed).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from codeprobe.core import sandbox

DISCLOSURE = (
    "codeprobe run executes an autonomous agent with "
    "--dangerously-skip-permissions plus mined third-party test/verifier "
    "scripts directly on this machine, with the invoking user's full "
    "filesystem, credential, and network access."
)


@dataclass(frozen=True)
class ContainmentPlan:
    """Resolved containment decision for the current run.

    ``engine`` is the container-engine binary path (docker or podman) and
    is only set for ``mode="container"`` plans; adapters use it to wrap
    the agent argv in a ``<engine> run`` invocation.
    """

    mode: Literal["sandboxed", "host-consented", "container"]
    engine: str | None = None


_active_plan: ContainmentPlan | None = None


def resolve_containment(uncontained: bool) -> ContainmentPlan:
    """Decide where this run's agent and mined scripts may execute.

    Returns a ``sandboxed`` plan when the environment is already contained
    (real container, or the user-set ``CODEPROBE_SANDBOX=1`` consent
    signal), a ``host-consented`` plan when the user passed
    ``--uncontained``, a ``container`` plan when a container engine is on
    PATH and the agent image is built (codeprobe-f7rl.5), and otherwise
    refuses with ``UNCONTAINED_REFUSED`` carrying the full disclosure.
    An engine without the agent image still refuses, with the ``docker
    build`` remediation appended.
    """
    if sandbox.is_sandboxed():
        return ContainmentPlan(mode="sandboxed")
    if uncontained:
        return ContainmentPlan(mode="host-consented")

    from codeprobe.sandbox import runner as container_runner

    agent_image = container_runner.agent_image_reference()
    engine = container_runner.detect_engine()
    if engine is not None and container_runner.image_available(engine, agent_image):
        return ContainmentPlan(mode="container", engine=engine)

    from codeprobe.cli.errors import PrescriptiveError

    build_hint = ""
    if engine is not None:
        build_tag = container_runner.agent_image_build_tag()
        build_hint = (
            " A container engine was found but the agent image "
            f"{agent_image!r} is not available locally. Pull a trusted "
            "published digest, or build it from the repo root with: docker build -f "
            "src/codeprobe/sandbox/Dockerfile.agent -t "
            f"{build_tag} . Override the reference with "
            f"{container_runner.AGENT_IMAGE_ENV} when using a private mirror."
        )
    raise PrescriptiveError(
        code="UNCONTAINED_REFUSED",
        message=(
            f"{DISCLOSURE} Run inside a container, or pass --uncontained "
            f"to accept this.{build_hint}"
        ),
        next_try_flag="--uncontained",
        next_try_value="",
        detail={"container_detected": False, "uncontained": False},
    )


def set_active_plan(plan: ContainmentPlan) -> None:
    """Record the resolved plan for downstream consumers (container beads)."""
    global _active_plan  # noqa: PLW0603
    _active_plan = plan


def active_plan() -> ContainmentPlan | None:
    """Return the plan recorded by :func:`set_active_plan`, if any."""
    return _active_plan
