"""Shared helpers for snapshot exporters.

Every exporter reads the same two inputs:

1. ``SNAPSHOT.json`` — the extended manifest (r14 + R18 fields).
2. ``summary/aggregate.json`` — per-task rollup ``{"entries": [...]}``.

This module centralises loading and per-row column projection so the
exporters stay focused on their output format.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

__all__ = [
    "load_manifest",
    "load_entries",
    "entry_columns",
    "project_row",
    "sanitize_formula",
]


# Characters that spreadsheet engines (Excel, Google Sheets, LibreOffice,
# Sigma, Looker) interpret as starting a formula or command when they appear
# as the first character of a cell. Writing a value like ``=HYPERLINK(...)``
# into a CSV/TSV leads to formula execution on the recipient's machine when
# the file is opened — a classic CSV-injection / spreadsheet-injection
# vector. See OWASP "CSV Injection".
_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")


def sanitize_formula(text: str) -> str:
    """Return ``text`` with a leading ``'`` prepended if it would be a formula.

    Any cell value whose first character is in :data:`_FORMULA_PREFIXES` is
    prefixed with a single quote so spreadsheet engines treat it as literal
    text rather than a formula. Non-string input is returned unchanged (the
    caller is expected to have stringified before calling).
    """
    if not text:
        return text
    if text[0] in _FORMULA_PREFIXES:
        return "'" + text
    return text


def load_manifest(snapshot_dir: Path) -> dict[str, Any]:
    """Return the parsed ``SNAPSHOT.json`` for ``snapshot_dir``.

    Raises :class:`FileNotFoundError` if the manifest is missing — exporters
    must operate on real snapshots, not empty directories.
    """
    manifest_path = Path(snapshot_dir) / "SNAPSHOT.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"snapshot manifest not found at {manifest_path}; "
            "run 'codeprobe snapshot create' first"
        )
    return json.loads(manifest_path.read_text())


def load_entries(snapshot_dir: Path) -> list[dict[str, Any]]:
    """Return per-task entries from ``summary/aggregate.json``.

    The file is written with the shape ``{"entries": [...]}`` by
    :func:`codeprobe.snapshot.create.create_snapshot`. Missing file or
    malformed shape yields an empty list — exporters must tolerate
    snapshots from experiments that produced no aggregate.
    """
    aggregate_path = Path(snapshot_dir) / "summary" / "aggregate.json"
    if not aggregate_path.is_file():
        return []
    try:
        doc = json.loads(aggregate_path.read_text())
    except json.JSONDecodeError:
        return []
    entries = doc.get("entries") if isinstance(doc, dict) else None
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def entry_columns(entries: list[dict[str, Any]]) -> list[str]:
    """Return the sorted union of keys across ``entries``.

    Stable ordering (alphabetical) keeps generated artefacts diff-friendly
    across runs with the same underlying data.
    """
    keys: set[str] = set()
    for entry in entries:
        keys.update(entry.keys())
    return sorted(keys)


def project_row(entry: dict[str, Any], columns: list[str]) -> list[Any]:
    """Return values from ``entry`` aligned to ``columns``.

    Missing keys are emitted as empty string so TSV/CSV writers produce
    well-shaped rows even on sparse entries.
    """
    return [entry.get(col, "") for col in columns]
