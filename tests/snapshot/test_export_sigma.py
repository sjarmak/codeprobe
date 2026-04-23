"""AC3: ``snapshot export --format sigma`` emits CSV + dbt-friendly schema.

We assert both files exist alongside each other, the CSV has a header
matching the union of entry keys, and the schema JSON uses dbt-friendly
type labels (string/number/boolean).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main as cli_main
from tests.snapshot._export_helpers import SYNTHETIC_ENTRIES, build_snapshot


def test_sigma_export_writes_csv_and_schema(tmp_path: Path) -> None:
    snap = build_snapshot(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["snapshot", "export", str(snap), "--format", "sigma"],
    )
    assert result.exit_code == 0, result.output

    csv_path = snap / "sigma_results.csv"
    schema_path = snap / "sigma_schema.json"
    assert csv_path.is_file()
    assert schema_path.is_file()

    with csv_path.open(encoding="utf-8") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    header = rows[0]
    body_rows = rows[1:]

    # The union of keys across SYNTHETIC_ENTRIES is our header set.
    expected_columns = sorted({k for e in SYNTHETIC_ENTRIES for k in e.keys()})
    assert header == expected_columns
    assert len(body_rows) == len(SYNTHETIC_ENTRIES)

    schema = json.loads(schema_path.read_text())
    assert "columns" in schema
    names = [c["name"] for c in schema["columns"]]
    assert names == expected_columns

    types_by_name = {c["name"]: c["type"] for c in schema["columns"]}
    # dbt-friendly types: booleans/numbers/strings only.
    assert types_by_name["passed"] == "boolean"
    assert types_by_name["score"] == "number"
    assert types_by_name["cost_usd"] == "number"
    assert types_by_name["latency_ms"] == "number"
    assert types_by_name["config"] == "string"
    assert types_by_name["task_id"] == "string"
    for col in schema["columns"]:
        assert isinstance(col["description"], str) and col["description"]


def test_sigma_export_cli_emits_path_payload(tmp_path: Path) -> None:
    snap = build_snapshot(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["snapshot", "export", str(snap), "--format", "sigma"],
    )
    assert result.exit_code == 0, result.output

    payload = json.loads(result.output)
    assert payload["format"] == "sigma"
    assert payload["csv"].endswith("sigma_results.csv")
    assert payload["schema"].endswith("sigma_schema.json")
