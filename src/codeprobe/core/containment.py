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

Anything else is a hard refusal (``UNCONTAINED_REFUSED``).

Later beads extend :func:`resolve_containment` with an automatic
``container`` mode (mined scripts and the agent subprocess execute inside a
container engine when one is available); downstream code consults the
decision via :func:`active_plan`. Pure policy enforcement, no model calls
(ZFC-allowed).
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
    """Resolved containment decision for the current run."""

    mode: Literal["sandboxed", "host-consented"]


_active_plan: ContainmentPlan | None = None


def resolve_containment(uncontained: bool) -> ContainmentPlan:
    """Decide where this run's agent and mined scripts may execute.

    Returns a ``sandboxed`` plan when the environment is already contained
    (real container, or the user-set ``CODEPROBE_SANDBOX=1`` consent
    signal), a ``host-consented`` plan when the user passed
    ``--uncontained``, and otherwise refuses with ``UNCONTAINED_REFUSED``
    carrying the full disclosure.
    """
    if sandbox.is_sandboxed():
        return ContainmentPlan(mode="sandboxed")
    if uncontained:
        return ContainmentPlan(mode="host-consented")

    from codeprobe.cli.errors import PrescriptiveError

    raise PrescriptiveError(
        code="UNCONTAINED_REFUSED",
        message=(
            f"{DISCLOSURE} Run inside a container, or pass --uncontained "
            "to accept this."
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
