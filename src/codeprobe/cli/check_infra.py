"""codeprobe check-infra — diagnostics for mined-task infrastructure.

Subcommands:

* ``codeprobe check-infra drift <task_dir>`` — compare the MCP capability set
  recorded in a mined task's ``metadata.json``
  (``mcp_capabilities_at_mine_time``) against the live
  ``codeprobe.mcp.capabilities.CAPABILITIES`` registry.
* ``codeprobe check-infra preamble-drift <task_dir>`` — alias that also flags
  drift between the mine-time capability snapshot and the live registry, from
  the preamble-authoring perspective. Same check, surface-level alias so the
  preamble-regen intent is explicit in CI logs.
* ``codeprobe check-infra offline`` — pre-flight for airgapped runs. Validates
  that each configured LLM backend's credential TTL exceeds an expected
  run duration, so a session won't mid-run fail when an STS token or GCP
  access token expires.

The drift check is structural IO plus set arithmetic — no heuristics, no
model calls. Meant to be wired into CI so a silent capability drift (e.g.
a new capability registered in the library after a task was mined) fails
loudly rather than silently changing the eval's tool surface.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import click

from codeprobe.mcp.capabilities import CAPABILITIES
from codeprobe.net.credential_ttl import (
    KNOWN_BACKENDS,
    CredentialTTLError,
    get_credential_ttl,
)


def _load_snapshot(metadata_path: Path) -> tuple[str, ...]:
    """Load ``mcp_capabilities_at_mine_time`` from a task's metadata.json.

    Raises ``click.ClickException`` on missing file, malformed JSON, or a
    malformed snapshot field — validate-or-die at the trust boundary.

    Reads from either the nested ``metadata.mcp_capabilities_at_mine_time``
    (current layout produced by the mining writer) or the top-level
    ``mcp_capabilities_at_mine_time`` (legacy layout) so ad-hoc test
    fixtures and older mined tasks both work.
    """
    if not metadata_path.is_file():
        raise click.ClickException(f"metadata.json not found at {metadata_path}")
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f"metadata.json at {metadata_path} is not valid JSON: {exc}"
        ) from exc

    # Prefer the nested metadata.<field> layout produced by the writer.
    raw = None
    meta = data.get("metadata") if isinstance(data, dict) else None
    if isinstance(meta, dict) and "mcp_capabilities_at_mine_time" in meta:
        raw = meta["mcp_capabilities_at_mine_time"]
    elif isinstance(data, dict) and "mcp_capabilities_at_mine_time" in data:
        raw = data["mcp_capabilities_at_mine_time"]
    else:
        raw = []

    if not isinstance(raw, list):
        raise click.ClickException(
            "metadata.mcp_capabilities_at_mine_time must be a JSON array"
        )
    for item in raw:
        if not isinstance(item, str):
            raise click.ClickException(
                "metadata.mcp_capabilities_at_mine_time entries must be strings"
            )
    return tuple(sorted(raw))


def _format_diff(
    snapshot: tuple[str, ...], live: tuple[str, ...]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return (added_since_mine, removed_since_mine)."""
    snap_set = set(snapshot)
    live_set = set(live)
    added = tuple(sorted(live_set - snap_set))
    removed = tuple(sorted(snap_set - live_set))
    return added, removed


def _run_drift_check(
    task_dir: str,
    *,
    fail_on_capability_drift: bool,
    allow_capability_drift: bool,
    banner: str,
) -> None:
    metadata_path = Path(task_dir) / "metadata.json"
    snapshot = _load_snapshot(metadata_path)
    live = tuple(sorted(CAPABILITIES.keys()))

    if snapshot == live:
        click.echo(f"OK — {len(live)} capabilities match snapshot.")
        return

    added, removed = _format_diff(snapshot, live)
    parts: list[str] = [banner]
    if added:
        parts.append(f"  added since mine: {', '.join(added)}")
    if removed:
        parts.append(f"  removed since mine: {', '.join(removed)}")
    parts.append(
        "  hint: regenerate the preamble and re-snapshot the task metadata"
    )
    message = "\n".join(parts)

    if allow_capability_drift:
        click.echo(f"WARNING: {message}", err=True)
        return

    if fail_on_capability_drift:
        raise click.ClickException(message)

    click.echo(f"WARNING: {message}", err=True)



@click.group(name="check-infra")
def check_infra() -> None:
    """Diagnostics for mined-task infrastructure (capability drift, etc.)."""


@check_infra.command("drift")
@click.argument("task_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--fail-on-capability-drift/--no-fail-on-capability-drift",
    default=True,
    help=(
        "Exit non-zero when the capability snapshot in metadata.json differs "
        "from the live CAPABILITIES registry. Default: enabled."
    ),
)
@click.option(
    "--allow-capability-drift",
    is_flag=True,
    default=False,
    help=(
        "Tolerate capability drift: emit a warning and exit 0 even when "
        "snapshots differ. Overrides --fail-on-capability-drift."
    ),
)
def drift_cmd(
    task_dir: str,
    fail_on_capability_drift: bool,
    allow_capability_drift: bool,
) -> None:
    """Compare metadata.json capability snapshot to live CAPABILITIES.

    TASK_DIR must be a directory containing a metadata.json produced by
    ``codeprobe mine``.
    """
    _run_drift_check(
        task_dir,
        fail_on_capability_drift=fail_on_capability_drift,
        allow_capability_drift=allow_capability_drift,
        banner="Capability drift detected:",
    )


@check_infra.command("preamble-drift")
@click.argument("task_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--fail-on-capability-drift/--no-fail-on-capability-drift",
    default=True,
    help=(
        "Exit non-zero when the mine-time capability snapshot differs from "
        "the live CAPABILITIES registry. Default: enabled."
    ),
)
@click.option(
    "--allow-capability-drift",
    is_flag=True,
    default=False,
    help=(
        "Tolerate drift: emit a warning and exit 0 even when snapshots differ. "
        "Overrides --fail-on-capability-drift."
    ),
)
def preamble_drift_cmd(
    task_dir: str,
    fail_on_capability_drift: bool,
    allow_capability_drift: bool,
) -> None:
    """Flag preamble drift when mine-time capabilities differ from live.

    Identical semantics to ``drift`` but surfaces the preamble-regeneration
    intent in CI logs — a preamble built against a stale capability set will
    describe tools the agent no longer has. The command fails loudly so CI
    catches the mismatch.
    """
    _run_drift_check(
        task_dir,
        fail_on_capability_drift=fail_on_capability_drift,
        allow_capability_drift=allow_capability_drift,
        banner="Preamble capability drift detected:",
    )


def _parse_duration(raw: str) -> timedelta:
    """Parse a human-friendly duration into :class:`timedelta`.

    Accepted forms: ``"1h"``, ``"30m"``, ``"45s"``, ``"90"`` (bare integer
    seconds). Raises :class:`click.BadParameter` on anything else so the
    CLI surfaces a precise error.
    """
    text = raw.strip().lower()
    if not text:
        raise click.BadParameter("duration must be non-empty")
    unit_seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text[-1] in unit_seconds:
        value_part = text[:-1]
        multiplier = unit_seconds[text[-1]]
    else:
        value_part = text
        multiplier = 1
    try:
        value = float(value_part)
    except ValueError as exc:
        raise click.BadParameter(
            f"duration {raw!r} is not a valid number with optional unit "
            f"(s, m, h, d)"
        ) from exc
    if value <= 0:
        raise click.BadParameter(f"duration {raw!r} must be positive")
    return timedelta(seconds=value * multiplier)


def _configured_backends() -> tuple[str, ...]:
    """Return the set of LLM backends to probe for credential TTL.

    Pulls from the r13 LLM registry and intersects with the TTL probes we
    know how to run. This keeps the pre-flight aligned with whichever
    backends are actually declared in ``model_registry.yaml``.
    """
    from codeprobe.llm import get_registry

    registry = get_registry()
    declared: set[str] = set()
    for logical in registry.logical_names():
        declared.update(registry.backends_for(logical))
    # Keep only those we can probe. Preserve KNOWN_BACKENDS order for
    # deterministic CLI output.
    return tuple(b for b in KNOWN_BACKENDS if b in declared)


@check_infra.command("offline")
@click.option(
    "--expected-run-duration",
    "expected_run_duration",
    default="1h",
    show_default=True,
    help=(
        "Minimum credential TTL required before an offline run starts. "
        "Accepts <N>s, <N>m, <N>h, <N>d. Each configured LLM backend's "
        "credential must outlive this value or the pre-flight fails."
    ),
)
@click.option(
    "--backend",
    "backend_filter",
    multiple=True,
    metavar="NAME",
    help=(
        "Restrict the check to these backend names (repeatable). "
        "Default: every backend declared in the LLM registry."
    ),
)
def offline_cmd(
    expected_run_duration: str,
    backend_filter: tuple[str, ...],
) -> None:
    """Pre-flight credential-TTL check for airgapped runs.

    For every configured LLM backend, look up the remaining credential
    lifetime. Fail (exit non-zero) if any backend's TTL is shorter than
    ``--expected-run-duration`` — the run would silently die mid-flight
    when the STS / access token expires.

    Backends with no expiration (Anthropic, user-managed openai_compat)
    or with no expiration advertised in the current environment are
    reported as ``no-expiry`` and pass the check.
    """
    expected = _parse_duration(expected_run_duration)

    configured = _configured_backends()
    if backend_filter:
        requested = {name.strip().lower() for name in backend_filter if name.strip()}
        unknown = sorted(requested - set(KNOWN_BACKENDS))
        if unknown:
            raise click.ClickException(
                f"Unknown backend(s): {', '.join(unknown)}. "
                f"Known: {', '.join(KNOWN_BACKENDS)}"
            )
        configured = tuple(b for b in configured if b in requested)

    if not configured:
        raise click.ClickException(
            "No LLM backends configured for offline pre-flight. "
            "Check model_registry.yaml or pass --backend explicitly."
        )

    failures: list[str] = []
    for backend in configured:
        try:
            ttl = get_credential_ttl(backend)
        except CredentialTTLError as exc:
            failures.append(
                f"{backend}: credential inspection failed — {exc}. "
                f"Remediation: refresh the credential for {backend!r} "
                f"before starting the offline run."
            )
            continue

        if ttl is None:
            click.echo(f"{backend}: no-expiry")
            continue

        if ttl <= timedelta(0):
            failures.append(
                f"{backend}: credential EXPIRED. "
                f"Remediation: refresh the {backend!r} credential "
                f"(e.g. re-issue the STS / access token) before running."
            )
            continue

        if ttl < expected:
            failures.append(
                f"{backend}: credential TTL {ttl} is shorter than "
                f"expected run duration {expected}. "
                f"Remediation: refresh the {backend!r} credential "
                f"so it outlives the run, or shorten "
                f"--expected-run-duration."
            )
            continue

        click.echo(f"{backend}: ttl={ttl} (>= {expected})")

    if failures:
        banner = "Offline pre-flight failed:"
        raise click.ClickException("\n  ".join([banner, *failures]))

    click.echo(
        f"OK — {len(configured)} backend(s) ready for an offline run "
        f"of up to {expected}."
    )


__all__ = ["check_infra"]
