"""interpret with no experiment must fail with a typed NO_EXPERIMENT error.

Regression tests for codeprobe-f7rl.13: ``codeprobe interpret .`` with no
experiment anywhere used to re-raise the raw ``FileNotFoundError`` from
``load_experiment`` — a full Python traceback, exit 1, no envelope, no
error code. The condition now maps to the same ``NO_EXPERIMENT``
diagnostic that ``run`` and the ``experiment`` subcommands emit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def test_interpret_json_emits_no_experiment_envelope(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(main, ["interpret", str(tmp_path), "--json"])
    assert result.exit_code == 2, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)

    envelope = json.loads(result.output)
    assert envelope["ok"] is False
    assert envelope["error"]["code"] == "NO_EXPERIMENT"
    assert envelope["error"]["kind"] == "diagnostic"
    assert envelope["error"]["terminal"] is True

    combined = result.output + (result.stderr or "")
    assert "Traceback" not in combined


def test_interpret_pretty_prints_diagnostic_without_traceback(
    runner: CliRunner, tmp_path: Path
) -> None:
    result = runner.invoke(main, ["interpret", str(tmp_path), "--no-json"])
    assert result.exit_code == 2
    assert result.exception is None or isinstance(result.exception, SystemExit)

    combined = result.output + (result.stderr or "")
    assert "NO_EXPERIMENT" in combined
    assert "experiment init" in combined
    assert "Traceback" not in combined
