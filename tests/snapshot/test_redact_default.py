"""AC2: `codeprobe snapshot create` defaults to --redact=hashes-only."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main as cli_main
from codeprobe.snapshot import PUBLISHABLE_DEFAULT


def test_publishable_default_constant_is_hashes_only() -> None:
    """The module-level default must be hashes-only."""
    assert PUBLISHABLE_DEFAULT == "hashes-only"


def test_cli_default_is_hashes_only(tmp_path: Path) -> None:
    """Invoke the CLI without --redact and check SNAPSHOT.json.mode."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello world\n")
    out = tmp_path / "snap"

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["snapshot", "create", str(src), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output

    manifest = json.loads((out / "SNAPSHOT.json").read_text())
    assert manifest["mode"] == "hashes-only"
    # No files/ subdir should exist in hashes-only mode.
    assert not (out / "files").exists()


def test_cli_rejects_contents_without_allow_flag(tmp_path: Path) -> None:
    """--redact=contents without --allow-source-in-export must exit non-zero."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello\n")
    out = tmp_path / "snap"

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "snapshot",
            "create",
            str(src),
            "--out",
            str(out),
            "--redact",
            "contents",
        ],
    )
    assert result.exit_code != 0
    assert "allow-source-in-export" in result.output


def test_cli_rejects_none_mode(tmp_path: Path) -> None:
    """'--redact=none' must not be a valid choice on the publishable surface."""
    src = tmp_path / "src"
    src.mkdir()

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "snapshot",
            "create",
            str(src),
            "--out",
            str(tmp_path / "snap"),
            "--redact",
            "none",
        ],
    )
    assert result.exit_code != 0
