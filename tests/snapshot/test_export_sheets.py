"""AC2: ``snapshot export --format sheets`` emits paste-ready TSV.

A Google Sheets paste only works if the output is strictly tab-separated,
has exactly one header row, and one row per task with no stray newlines
inside cells. We assert all three.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main as cli_main
from tests.snapshot._export_helpers import SYNTHETIC_ENTRIES, build_snapshot


def test_sheets_export_is_tab_separated(tmp_path: Path) -> None:
    snap = build_snapshot(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["snapshot", "export", str(snap), "--format", "sheets"],
    )
    assert result.exit_code == 0, result.output

    tsv_path = snap / "sheets.tsv"
    assert tsv_path.is_file()
    text = tsv_path.read_text()

    # Strip final newline; the artefact is terminator-normalised.
    lines = text.rstrip("\n").split("\n")
    assert len(lines) == 1 + len(SYNTHETIC_ENTRIES)

    expected_columns = sorted({k for e in SYNTHETIC_ENTRIES for k in e.keys()})
    header_cells = lines[0].split("\t")
    assert header_cells == expected_columns

    # Every data row has exactly one cell per column.
    for row in lines[1:]:
        cells = row.split("\t")
        assert len(cells) == len(expected_columns)
        # No stray tabs/newlines inside cells.
        for cell in cells:
            assert "\n" not in cell
            assert "\r" not in cell


def test_sheets_export_preserves_values(tmp_path: Path) -> None:
    snap = build_snapshot(tmp_path)
    target = tmp_path / "paste.tsv"

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "snapshot",
            "export",
            str(snap),
            "--format",
            "sheets",
            "--out",
            str(target),
            "--no-json",
        ],
    )
    assert result.exit_code == 0, result.output

    text = target.read_text()
    # Round-trip a known value (task id) through the TSV.
    assert "task-001" in text
    assert "task-002" in text
    assert "baseline" in text

    payload = json.loads(result.output)
    assert payload["format"] == "sheets"
    assert Path(payload["out"]).resolve() == target.resolve()
