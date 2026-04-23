"""codeprobe check-infra — diagnostics for mined-task infrastructure.

Currently exposes one subcommand:

* ``codeprobe check-infra drift`` — compare the MCP capability set recorded
  in a mined task's ``metadata.json`` (``mcp_capabilities_at_mine_time``)
  against the live ``codeprobe.mcp.capabilities.CAPABILITIES`` registry.

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
    """
    if not metadata_path.is_file():
        raise click.ClickException(f"metadata.json not found at {metadata_path}")
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f"metadata.json at {metadata_path} is not valid JSON: {exc}"
        ) from exc

    meta = data.get("metadata")
    if not isinstance(meta, dict):
        raise click.ClickException(
            f"metadata.json at {metadata_path} missing 'metadata' object"
        )
    raw = meta.get("mcp_capabilities_at_mine_time", [])
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
    metadata_path = Path(task_dir) / "metadata.json"
    snapshot = _load_snapshot(metadata_path)
    live = tuple(sorted(CAPABILITIES.keys()))

    if snapshot == live:
        click.echo(f"OK — {len(live)} capabilities match snapshot.")
        return

    added, removed = _format_diff(snapshot, live)
    parts: list[str] = ["Capability drift detected:"]
    if added:
        parts.append(f"  added since mine: {', '.join(added)}")
    if removed:
        parts.append(f"  removed since mine: {', '.join(removed)}")
    message = "\n".join(parts)

    if allow_capability_drift:
        click.echo(f"WARNING: {message}", err=True)
        return

    if fail_on_capability_drift:
        raise click.ClickException(message)

    click.echo(f"WARNING: {message}", err=True)


__all__ = ["check_infra"]
