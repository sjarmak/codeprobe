"""BC-H-02: CSV/TSV exporters must guard against spreadsheet formula injection.

When an entry string field starts with ``=``, ``+``, ``-``, ``@``, ``\\t``,
or ``\\r``, spreadsheet engines (Google Sheets, Excel, LibreOffice Calc,
Sigma, Looker) interpret the cell as a formula or command on open — a
classic CSV-injection vector.

Both exporters must prepend a literal ``'`` so the cell is rendered as text.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main as cli_main
from tests.snapshot._export_helpers import build_snapshot

_MALICIOUS_ENTRIES: list[dict[str, object]] = [
    {
        "config": "baseline",
        "task_id": "=HYPERLINK(\"https://evil.example/steal\",\"click\")",
        "notes": "+1234567",
        "cmd": "-sum(A1:A10)",
        "tag": "@SUM(1+2)",
        "label": "\tnormal",
        "cr_field": "\rhidden",
        "safe": "no_formula_here",
    },
]


def _snapshot_with_entries(
    tmp_path: Path, entries: list[dict[str, object]]
) -> Path:
    """Build a snapshot and overwrite its aggregate with malicious rows."""
    snap = build_snapshot(tmp_path)
    aggregate = snap / "summary" / "aggregate.json"
    aggregate.write_text(
        json.dumps({"entries": entries}, indent=2, sort_keys=True)
    )
    return snap


def test_sheets_export_sanitizes_formula_prefixes(tmp_path: Path) -> None:
    snap = _snapshot_with_entries(tmp_path, _MALICIOUS_ENTRIES)
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
        ],
    )
    assert result.exit_code == 0, result.output

    text = target.read_text()
    lines = text.rstrip("\n").split("\n")
    header = lines[0].split("\t")
    data = lines[1].split("\t")
    row = dict(zip(header, data, strict=False))

    # Every would-be-formula cell must start with a literal single quote so
    # Google Sheets renders it as text rather than evaluating it.
    assert row["task_id"].startswith("'=HYPERLINK("), row["task_id"]
    assert row["notes"].startswith("'+"), row["notes"]
    assert row["cmd"].startswith("'-"), row["cmd"]
    assert row["tag"].startswith("'@"), row["tag"]
    # Tab and CR are stripped to space by the TSV cleaner, so after
    # stripping the leading prefix they are space + remainder.
    assert row["label"].startswith("' "), row["label"]
    assert row["cr_field"].startswith("' "), row["cr_field"]

    # Safe values are untouched.
    assert row["safe"] == "no_formula_here"

    # And critically: no unquoted formula remains in the output.
    for bad in ("=HYPERLINK(", "+1234567", "-sum(", "@SUM("):
        assert f"\t{bad}" not in text, f"unquoted formula leaked: {bad!r}"
        assert not text.split("\n")[1].startswith(bad)


def test_sigma_export_sanitizes_formula_prefixes(tmp_path: Path) -> None:
    snap = _snapshot_with_entries(tmp_path, _MALICIOUS_ENTRIES)
    out_dir = tmp_path / "sigma_out"

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "snapshot",
            "export",
            str(snap),
            "--format",
            "sigma",
            "--out",
            str(out_dir),
        ],
    )
    assert result.exit_code == 0, result.output

    csv_path = out_dir / "sigma_results.csv"
    with csv_path.open(encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        rows = list(reader)

    assert len(rows) == 1
    row = rows[0]

    assert row["task_id"].startswith("'=HYPERLINK("), row["task_id"]
    assert row["notes"].startswith("'+"), row["notes"]
    assert row["cmd"].startswith("'-"), row["cmd"]
    assert row["tag"].startswith("'@"), row["tag"]
    assert row["label"].startswith("'\t"), row["label"]
    # ``\r`` inside a CSV field is normalised to ``\n`` by csv.DictReader —
    # the important invariant is that the cell starts with a single-quote
    # *before* the whitespace, so spreadsheets render it as literal text.
    assert row["cr_field"].startswith("'"), row["cr_field"]
    assert row["cr_field"][1:2] in ("\r", "\n"), row["cr_field"]
    assert row["safe"] == "no_formula_here"

    # Raw file must not contain an unquoted formula prefix at the start of a field.
    raw = csv_path.read_text()
    # csv.QUOTE_MINIMAL won't quote our "'=..." strings specially, but a
    # would-be-formula would appear as ",=HYPERLINK(" or at field start. We
    # assert neither of those patterns is present.
    assert ",=HYPERLINK(" not in raw
    assert raw.splitlines()[1].split(",")[0] != "=HYPERLINK(…"


def test_sanitize_formula_is_idempotent() -> None:
    """Guard against accidental double-prefixing when values already start with '."""
    from codeprobe.snapshot.exporters._common import sanitize_formula

    assert sanitize_formula("=bad") == "'=bad"
    # Already-prefixed literal — do not double-prefix.
    assert sanitize_formula("'=ok") == "'=ok"
    assert sanitize_formula("safe") == "safe"
    assert sanitize_formula("") == ""
