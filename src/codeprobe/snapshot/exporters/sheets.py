"""Google Sheets paste-ready TSV exporter.

Produces a tab-separated-values block with one header row and one row per
task entry. Users copy the file contents and paste directly into a Sheet
(Google Sheets auto-parses tabs into cells).

Any tabs or newlines inside values are replaced with single spaces so the
paste-round-trip is lossless at the row/column level.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codeprobe.snapshot.exporters._common import (
    entry_columns,
    load_entries,
    load_manifest,
    project_row,
)

__all__ = ["export_sheets"]


def _cell(value: Any) -> str:
    """Return ``value`` rendered as a single-line TSV cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        rendered = str(value)
    elif isinstance(value, str):
        rendered = value
    else:
        rendered = json.dumps(value, sort_keys=True, separators=(",", ":"))
    # Strip embedded tabs/newlines so the TSV row/col grid stays intact.
    return rendered.replace("\t", " ").replace("\n", " ").replace("\r", " ")


def export_sheets(snapshot_dir: Path, out_path: Path) -> Path:
    """Write a TSV block summarising ``snapshot_dir`` to ``out_path``.

    Returns the written path. Uses ``\n`` line terminators so pasting into
    Sheets on any platform produces one row per line.
    """
    snapshot_dir = Path(snapshot_dir)
    out_path = Path(out_path)

    load_manifest(snapshot_dir)
    entries = load_entries(snapshot_dir)
    columns = entry_columns(entries)

    lines: list[str] = []
    lines.append("\t".join(columns))
    for entry in entries:
        lines.append("\t".join(_cell(v) for v in project_row(entry, columns)))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return out_path
