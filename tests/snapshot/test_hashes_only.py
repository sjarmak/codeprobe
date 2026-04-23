"""AC3: hashes-only snapshot does not leak source content.

Create a source directory that contains a distinctive symbol string, run the
snapshot CLI in hashes-only mode, then grep every file under the output
directory for that symbol. Zero hits expected.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main as cli_main

_SYMBOL = "HELLO_SNAPSHOT_PROBE_ABC_SENTINEL_XYZ"


def test_hashes_only_zero_symbol_hits(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.py").write_text(f"def fn():\n    return '{_SYMBOL}'\n")
    (src / "sub" / "b.md").write_text(f"Another mention of {_SYMBOL} here.\n")
    (src / "sub" / "c.bin").write_bytes(b"raw bytes " + _SYMBOL.encode() + b" end")

    out = tmp_path / "snap"

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["snapshot", "create", str(src), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output

    # Grep every file under the snapshot dir for the symbol.
    hits: list[tuple[str, bytes]] = []
    for p in out.rglob("*"):
        if not p.is_file():
            continue
        body = p.read_bytes()
        if _SYMBOL.encode() in body:
            hits.append((str(p), body))
    assert hits == [], f"hashes-only snapshot leaked source symbol: {hits!r}"


def test_hashes_only_records_correct_sha256(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    body = b"predictable body bytes\n"
    (src / "known.txt").write_bytes(body)
    expected_sha = hashlib.sha256(body).hexdigest()

    out = tmp_path / "snap"
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["snapshot", "create", str(src), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output

    manifest = json.loads((out / "SNAPSHOT.json").read_text())
    found = [f for f in manifest["files"] if f["path"] == "known.txt"]
    assert len(found) == 1
    assert found[0]["sha256"] == expected_sha
    assert found[0]["size"] == len(body)
    # No body path in hashes-only mode.
    assert found[0].get("redacted_body") is None


def test_hashes_only_no_files_subdir(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.txt").write_text("x")
    out = tmp_path / "snap"

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["snapshot", "create", str(src), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output
    # hashes-only must never materialize a files/ subtree.
    assert not (out / "files").exists()
