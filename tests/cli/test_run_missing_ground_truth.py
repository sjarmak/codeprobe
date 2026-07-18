"""Regression test for the missing-ground-truth preflight check (codeprobe-yxex).

A stale or interrupted `codeprobe mine` run can leave an artifact_eval/dual
task without tests/ground_truth.json. Before this check, `codeprobe run`
would score every trial on such a task ``verifier_error`` — indistinguishable
from an agent failure — instead of rejecting it loudly up front.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from codeprobe.cli.errors import DiagnosticError
from codeprobe.cli.run_cmd import _check_ground_truth_present


def _make_task_dir(
    parent: Path,
    name: str,
    *,
    verification_mode: str = "test_script",
) -> Path:
    """Create a minimal task directory with instruction.md and task.toml."""
    td = parent / name
    td.mkdir()
    (td / "instruction.md").write_text(f"# {name}\nDo the thing.\n")
    (td / "task.toml").write_text(
        textwrap.dedent(f"""\
            [task]
            id = "{name}"
            repo = "test/repo"

            [metadata]
            name = "{name}"

            [verification]
            type = "test_script"
            command = "bash tests/test.sh"
            verification_mode = "{verification_mode}"
            """)
    )
    return td


class TestCheckGroundTruthPresent:
    def test_artifact_eval_missing_ground_truth_raises(self, tmp_path: Path) -> None:
        td = _make_task_dir(tmp_path, "task-1", verification_mode="artifact_eval")

        with pytest.raises(DiagnosticError) as exc_info:
            _check_ground_truth_present([td], str(tmp_path))

        err = exc_info.value
        assert err.code == "MISSING_GROUND_TRUTH"
        assert "task-1" in err.message

    def test_dual_missing_ground_truth_raises(self, tmp_path: Path) -> None:
        td = _make_task_dir(tmp_path, "task-2", verification_mode="dual")

        with pytest.raises(DiagnosticError) as exc_info:
            _check_ground_truth_present([td], str(tmp_path))

        assert exc_info.value.code == "MISSING_GROUND_TRUTH"
        assert "task-2" in exc_info.value.detail["missing_ground_truth_tasks"]

    def test_artifact_eval_with_ground_truth_does_not_raise(self, tmp_path: Path) -> None:
        td = _make_task_dir(tmp_path, "task-3", verification_mode="artifact_eval")
        (td / "tests").mkdir()
        (td / "tests" / "ground_truth.json").write_text("{}")

        _check_ground_truth_present([td], str(tmp_path))

    def test_test_script_missing_ground_truth_does_not_raise(self, tmp_path: Path) -> None:
        """The vast majority of SDLC tasks have no ground_truth.json at all."""
        td = _make_task_dir(tmp_path, "task-4", verification_mode="test_script")

        _check_ground_truth_present([td], str(tmp_path))

    def test_legacy_root_ground_truth_counts_as_present(self, tmp_path: Path) -> None:
        td = _make_task_dir(tmp_path, "task-5", verification_mode="dual")
        (td / "ground_truth.json").write_text("{}")

        _check_ground_truth_present([td], str(tmp_path))

    def test_skips_tasks_without_metadata(self, tmp_path: Path) -> None:
        td_bare = tmp_path / "bare"
        td_bare.mkdir()
        (td_bare / "instruction.md").write_text("# bare\n")

        _check_ground_truth_present([td_bare], str(tmp_path))
