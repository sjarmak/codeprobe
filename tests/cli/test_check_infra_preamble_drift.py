"""Tests for `codeprobe check-infra` preamble drift detection.

AC #5 of r12-capability-preambles: when the mine-time capability set
differs from the live capability set and the task has not been
regenerated, check-infra must exit non-zero.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.mcp.capabilities import CAPABILITIES


def _write_task_metadata(
    task_dir: Path,
    mine_time_capabilities: list[str] | None,
) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "id": "task-r12-drift",
        "repo": "acme/widgets",
        "metadata": {
            "name": "Test task",
        },
    }
    if mine_time_capabilities is not None:
        payload["metadata"]["mcp_capabilities_at_mine_time"] = mine_time_capabilities
    (task_dir / "metadata.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


def test_check_infra_fails_on_stale_capability_set(tmp_path: Path) -> None:
    task_dir = tmp_path / "task-stale"
    # Deliberately stale: only KEYWORD_SEARCH, missing the others.
    _write_task_metadata(task_dir, mine_time_capabilities=["KEYWORD_SEARCH"])

    runner = CliRunner()
    result = runner.invoke(main, ["check-infra", "drift", str(task_dir)])

    assert result.exit_code != 0, (
        f"expected non-zero exit on drift; got {result.exit_code}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    combined = (result.stdout or "") + (result.stderr or "")
    assert "drift" in combined.lower()


def test_check_infra_preamble_drift_subcommand_fails_on_stale(
    tmp_path: Path,
) -> None:
    """The preamble-drift alias exits non-zero when the snapshot is stale,
    pinning AC #5: preamble regeneration is required after capability changes."""
    task_dir = tmp_path / "task-preamble-stale"
    _write_task_metadata(task_dir, mine_time_capabilities=["KEYWORD_SEARCH"])

    runner = CliRunner()
    result = runner.invoke(main, ["check-infra", "preamble-drift", str(task_dir)])

    assert result.exit_code != 0, (
        f"expected non-zero exit on preamble drift; got {result.exit_code}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    combined = (result.stdout or "") + (result.stderr or "")
    assert "preamble" in combined.lower()


def test_check_infra_passes_when_capability_set_matches(tmp_path: Path) -> None:
    task_dir = tmp_path / "task-ok"
    _write_task_metadata(
        task_dir,
        mine_time_capabilities=sorted(CAPABILITIES.keys()),
    )

    runner = CliRunner()
    # --no-json preserves the legacy "OK" pretty surface the assertion
    # expects; CliRunner is non-TTY so the envelope default would otherwise
    # be emitted.
    result = runner.invoke(
        main, ["check-infra", "drift", str(task_dir), "--no-json"]
    )

    assert result.exit_code == 0, (
        f"expected exit 0 when capabilities match; got {result.exit_code}\n"
        f"stdout={result.stdout!r}\nstderr={result.stderr!r}"
    )
    assert "OK" in (result.stdout or "")


def test_check_infra_allow_drift_flag(tmp_path: Path) -> None:
    """--allow-capability-drift downgrades the failure to a warning."""
    task_dir = tmp_path / "task-allow-drift"
    _write_task_metadata(task_dir, mine_time_capabilities=["KEYWORD_SEARCH"])

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "check-infra",
            "drift",
            "--allow-capability-drift",
            str(task_dir),
            "--no-json",
        ],
    )

    assert result.exit_code == 0
    combined = (result.stdout or "") + (result.stderr or "")
    assert "WARNING" in combined


def test_check_infra_fails_on_missing_metadata(tmp_path: Path) -> None:
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(main, ["check-infra", "drift", str(empty_dir)])
    assert result.exit_code != 0
