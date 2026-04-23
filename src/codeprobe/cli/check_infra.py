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

The drift check is structural IO plus set arithmetic — no heuristics, no
model calls. Meant to be wired into CI so a silent capability drift (e.g.
a new capability registered in the library after a task was mined) fails
loudly rather than silently changing the eval's tool surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from codeprobe.mcp.capabilities import CAPABILITIES


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


__all__ = ["check_infra"]
