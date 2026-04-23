"""Sigma / Looker exporter — CSV results + dbt-friendly schema JSON.

Writes two files next to each other:

- ``sigma_results.csv`` — header row (sorted column names) + one row per task
  entry. Values are stringified; the schema file describes each column's
  logical dbt-style type.
- ``sigma_schema.json`` — ``{"columns": [{"name", "type", "description"}, ...]}``
  suitable for dropping into a dbt model's ``schema.yml`` by analysts.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from codeprobe.snapshot.exporters._common import (
    entry_columns,
    load_entries,
    load_manifest,
    project_row,
)

__all__ = ["export_sigma"]

_CSV_NAME = "sigma_results.csv"
_SCHEMA_NAME = "sigma_schema.json"


def _infer_column_type(entries: list[dict[str, Any]], column: str) -> str:
    """Return a dbt-friendly type label inferred from ``column`` values.

    The first non-null value wins — mixed-type columns will be reported as
    the type of the first observation, which is good enough for Sigma's
    schema hints and matches the behaviour of `dbt`'s `run_query` preview.
    """
    for entry in entries:
        if column not in entry:
            continue
        value = entry[column]
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            return "boolean"
        if isinstance(value, (int, float)):
            return "number"
        return "string"
    return "string"


def _build_schema(
    entries: list[dict[str, Any]], columns: list[str]
) -> dict[str, Any]:
    return {
        "columns": [
            {
                "name": col,
                "type": _infer_column_type(entries, col),
                "description": f"Column '{col}' from codeprobe snapshot aggregate.",
            }
            for col in columns
        ]
    }


def _stringify(value: Any) -> str:
    """Return a CSV-safe string representation of ``value``."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return value
    # Dict/list values are rendered as compact JSON so downstream SQL can
    # still parse them via json_extract if needed.
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def export_sigma(snapshot_dir: Path, out_dir: Path) -> tuple[Path, Path]:
    """Write ``sigma_results.csv`` and ``sigma_schema.json`` under ``out_dir``.

    Returns ``(csv_path, schema_path)``. ``out_dir`` is created if missing.
    """
    snapshot_dir = Path(snapshot_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Touch the manifest to surface missing-snapshot errors early — we do
    # not need any fields from it for the CSV output itself.
    load_manifest(snapshot_dir)
    entries = load_entries(snapshot_dir)
    columns = entry_columns(entries)

    csv_path = out_dir / _CSV_NAME
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(columns)
        for entry in entries:
            writer.writerow([_stringify(v) for v in project_row(entry, columns)])

    schema_path = out_dir / _SCHEMA_NAME
    schema_path.write_text(
        json.dumps(_build_schema(entries, columns), indent=2, sort_keys=True)
    )

    return csv_path, schema_path
