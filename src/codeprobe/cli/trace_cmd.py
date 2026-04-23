"""``codeprobe trace`` subcommand group — export the trace store.

Currently exposes one command: ``codeprobe trace export <run-dir>``
which emits JSONL to stdout (or ``--output``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from codeprobe.trace.store import export_jsonl


@click.group("trace")
def trace() -> None:
    """Inspect the per-experiment trace.db."""


@trace.command("export")
@click.argument("run_dir", type=click.Path(exists=True, file_okay=False))
@click.option(
    "--output",
    "-o",
    "output_path",
    type=click.Path(dir_okay=False),
    default=None,
    help="Write JSONL to this file. Default: stdout.",
)
def export_cmd(run_dir: str, output_path: str | None) -> None:
    """Export events from RUN_DIR/trace.db as JSONL (one event per line)."""
    db_path = Path(run_dir) / "trace.db"
    if not db_path.is_file():
        raise click.ClickException(f"No trace.db at {db_path}")

    if output_path is None:
        rows = export_jsonl(db_path, sys.stdout)
    else:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", encoding="utf-8") as fh:
            rows = export_jsonl(db_path, fh)

    click.echo(f"Exported {rows} event(s)", err=True)
