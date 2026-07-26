"""Tests for the validate command."""

from __future__ import annotations

import json
import shutil
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.cli.validate_cmd import run_validate


@pytest.fixture()
def valid_task_dir(tmp_path: Path) -> Path:
    """Create a minimal valid task directory (test_script mode)."""
    (tmp_path / "instruction.md").write_text("# Task\nDo something useful.\n")
    (tmp_path / "task.toml").write_text(
        '[metadata]\nname = "test-task"\ntask_type = "sdlc_code_change"\n\n'
        '[verification]\nverification_mode = "test_script"\n'
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_sh = tests_dir / "test.sh"
    test_sh.write_text("#!/bin/bash\nexit 0\n")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return tmp_path


@pytest.fixture()
def artifact_eval_task_dir(tmp_path: Path) -> Path:
    """Create a valid artifact_eval task directory."""
    (tmp_path / "instruction.md").write_text("# Task\nAnswer a question.\n")
    (tmp_path / "metadata.json").write_text(
        json.dumps(
            {
                "name": "artifact-task",
                "task_type": "architecture_comprehension",
                "verification_mode": "artifact_eval",
            }
        )
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "ground_truth.json").write_text(
        json.dumps({"answer_type": "file_list", "expected": ["a.py", "b.py"]})
    )
    return tmp_path


@pytest.fixture()
def dual_task_dir(tmp_path: Path) -> Path:
    """Create a valid dual-mode task directory."""
    (tmp_path / "instruction.md").write_text("# Dual task\n")
    (tmp_path / "task.toml").write_text(
        '[metadata]\nname = "dual-task"\n\n'
        '[verification]\nverification_mode = "dual"\n'
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    test_sh = tests_dir / "test.sh"
    test_sh.write_text("#!/bin/bash\nexit 0\n")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (tests_dir / "ground_truth.json").write_text(
        json.dumps({"answer_type": "boolean", "answer": True})
    )
    return tmp_path


class TestRunValidate:
    """Unit tests for run_validate function."""

    def test_valid_task_all_pass(self, valid_task_dir: Path) -> None:
        results = run_validate(valid_task_dir)
        assert all(r.passed for r in results), [r for r in results if not r.passed]

    def test_missing_instruction(self, tmp_path: Path) -> None:
        (tmp_path / "task.toml").write_text('[metadata]\nname = "x"\n')
        results = run_validate(tmp_path)
        instr = next(r for r in results if "instruction" in r.name)
        assert not instr.passed
        assert "instruction.md" in instr.detail

    def test_empty_instruction(self, tmp_path: Path) -> None:
        (tmp_path / "instruction.md").write_text("")
        (tmp_path / "task.toml").write_text('[metadata]\nname = "x"\n')
        results = run_validate(tmp_path)
        instr = next(r for r in results if "instruction" in r.name)
        assert not instr.passed
        assert "empty" in instr.detail

    def test_missing_metadata(self, tmp_path: Path) -> None:
        (tmp_path / "instruction.md").write_text("# Task\n")
        results = run_validate(tmp_path)
        meta = next(r for r in results if "metadata" in r.name)
        assert not meta.passed
        assert "neither" in meta.detail

    def test_bad_toml(self, tmp_path: Path) -> None:
        (tmp_path / "instruction.md").write_text("# Task\n")
        (tmp_path / "task.toml").write_text("this is not valid toml [[[")
        results = run_validate(tmp_path)
        meta = next(r for r in results if "metadata" in r.name)
        assert not meta.passed
        assert "parse error" in meta.detail

    def test_bad_json_metadata(self, tmp_path: Path) -> None:
        (tmp_path / "instruction.md").write_text("# Task\n")
        (tmp_path / "metadata.json").write_text("{bad json")
        results = run_validate(tmp_path)
        meta = next(r for r in results if "metadata" in r.name)
        assert not meta.passed
        assert "parse error" in meta.detail

    def test_metadata_json_valid(self, artifact_eval_task_dir: Path) -> None:
        results = run_validate(artifact_eval_task_dir)
        meta = next(r for r in results if "metadata" in r.name)
        assert meta.passed

    def test_invalid_task_type(self, tmp_path: Path) -> None:
        (tmp_path / "instruction.md").write_text("# Task\n")
        (tmp_path / "metadata.json").write_text(
            json.dumps({"task_type": "bogus_type", "verification_mode": "test_script"})
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        test_sh = tests_dir / "test.sh"
        test_sh.write_text("#!/bin/bash\nexit 0\n")
        test_sh.chmod(test_sh.stat().st_mode | stat.S_IXUSR)
        results = run_validate(tmp_path)
        tt = next(r for r in results if "task_type" in r.name)
        assert not tt.passed
        assert "bogus_type" in tt.detail

    def test_invalid_verification_mode(self, tmp_path: Path) -> None:
        (tmp_path / "instruction.md").write_text("# Task\n")
        (tmp_path / "metadata.json").write_text(
            json.dumps({"verification_mode": "invalid_mode"})
        )
        results = run_validate(tmp_path)
        vm = next(r for r in results if "verification_mode" in r.name)
        assert not vm.passed
        assert "invalid_mode" in vm.detail

    def test_test_script_missing(self, tmp_path: Path) -> None:
        (tmp_path / "instruction.md").write_text("# Task\n")
        (tmp_path / "task.toml").write_text(
            '[verification]\nverification_mode = "test_script"\n'
        )
        results = run_validate(tmp_path)
        ts = next(r for r in results if "test.sh" in r.name)
        assert not ts.passed

    def test_test_script_not_executable(self, tmp_path: Path) -> None:
        (tmp_path / "instruction.md").write_text("# Task\n")
        (tmp_path / "task.toml").write_text(
            '[verification]\nverification_mode = "test_script"\n'
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        test_sh = tests_dir / "test.sh"
        test_sh.write_text("#!/bin/bash\nexit 0\n")
        # Explicitly remove execute bits
        test_sh.chmod(stat.S_IRUSR | stat.S_IWUSR)
        results = run_validate(tmp_path)
        ts = next(r for r in results if "test.sh" in r.name)
        assert not ts.passed
        assert "not executable" in ts.detail

    def test_artifact_eval_valid(self, artifact_eval_task_dir: Path) -> None:
        results = run_validate(artifact_eval_task_dir)
        assert all(r.passed for r in results), [r for r in results if not r.passed]

    def test_artifact_eval_missing_ground_truth(self, tmp_path: Path) -> None:
        (tmp_path / "instruction.md").write_text("# Task\n")
        (tmp_path / "metadata.json").write_text(
            json.dumps({"verification_mode": "artifact_eval"})
        )
        results = run_validate(tmp_path)
        gt = next(r for r in results if "ground_truth" in r.name)
        assert not gt.passed

    def test_artifact_eval_ground_truth_no_answer_type(self, tmp_path: Path) -> None:
        (tmp_path / "instruction.md").write_text("# Task\n")
        (tmp_path / "metadata.json").write_text(
            json.dumps({"verification_mode": "artifact_eval"})
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "ground_truth.json").write_text(json.dumps({"expected": []}))
        results = run_validate(tmp_path)
        gt = next(r for r in results if "ground_truth" in r.name)
        assert not gt.passed
        assert "answer_type" in gt.detail

    def test_artifact_eval_ground_truth_bad_json(self, tmp_path: Path) -> None:
        (tmp_path / "instruction.md").write_text("# Task\n")
        (tmp_path / "metadata.json").write_text(
            json.dumps({"verification_mode": "artifact_eval"})
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        (tests_dir / "ground_truth.json").write_text("{bad")
        results = run_validate(tmp_path)
        gt = next(r for r in results if "ground_truth" in r.name)
        assert not gt.passed
        assert "parse error" in gt.detail

    def test_dual_mode_valid(self, dual_task_dir: Path) -> None:
        results = run_validate(dual_task_dir)
        assert all(r.passed for r in results), [r for r in results if not r.passed]

    def test_dual_mode_needs_both(self, tmp_path: Path) -> None:
        """Dual mode should fail when ground_truth.json is missing."""
        (tmp_path / "instruction.md").write_text("# Task\n")
        (tmp_path / "task.toml").write_text(
            '[verification]\nverification_mode = "dual"\n'
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        test_sh = tests_dir / "test.sh"
        test_sh.write_text("#!/bin/bash\nexit 0\n")
        test_sh.chmod(test_sh.stat().st_mode | stat.S_IXUSR)
        # No ground_truth.json
        results = run_validate(tmp_path)
        gt = next(r for r in results if "ground_truth" in r.name)
        assert not gt.passed


class TestAnswerTxtDrift:
    """answer.txt drift check (codeprobe-w8pg)."""

    def _drift_results(self, results: list) -> list:
        return [r for r in results if r.name == "answer.txt drift"]

    def _make_dual_task(
        self,
        tmp_path: Path,
        *,
        gt_payload: dict,
        gt_in_tests_dir: bool = True,
        verification_mode: str = "dual",
    ) -> Path:
        """Build a minimal task dir with a configurable ground_truth.json.

        ``gt_in_tests_dir`` toggles between the dual-mode layout
        (``tests/ground_truth.json``) and the legacy mined-task layout
        (``ground_truth.json`` at task root).
        """
        (tmp_path / "instruction.md").write_text("# Task\n")
        (tmp_path / "task.toml").write_text(
            '[metadata]\nname = "t"\n\n'
            f'[verification]\nverification_mode = "{verification_mode}"\n'
        )
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir()
        ts = tests_dir / "test.sh"
        ts.write_text("#!/bin/bash\nexit 0\n")
        ts.chmod(ts.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

        gt_path = (tests_dir if gt_in_tests_dir else tmp_path) / "ground_truth.json"
        gt_path.write_text(json.dumps(gt_payload))
        return tmp_path

    def test_no_answer_txt_skips_check(self, dual_task_dir: Path) -> None:
        """Tasks without answer.txt should not surface a drift result."""
        results = run_validate(dual_task_dir)
        assert self._drift_results(results) == []

    def test_file_list_match_dual_layout(self, tmp_path: Path) -> None:
        task = self._make_dual_task(
            tmp_path,
            gt_payload={
                "answer_type": "file_list",
                "answer": ["src/a.py", "src/b.py"],
            },
        )
        (task / "answer.txt").write_text("src/a.py\nsrc/b.py\n")
        results = run_validate(task)
        drift = self._drift_results(results)
        assert len(drift) == 1
        assert drift[0].passed
        assert "matches ground_truth" in drift[0].detail

    def test_file_list_drift_warn(self, tmp_path: Path) -> None:
        """Voxa-class regression: stale answer.txt vs updated ground_truth."""
        task = self._make_dual_task(
            tmp_path,
            gt_payload={
                "answer_type": "file_list",
                "answer": ["src/new.py", "src/keep.py"],
            },
        )
        (task / "answer.txt").write_text("src/keep.py\nsrc/old.py\n")
        results = run_validate(task)
        drift = self._drift_results(results)
        assert len(drift) == 1
        assert drift[0].passed  # advisory, never fails the run
        assert drift[0].detail.startswith("warn:")
        assert "jaccard" in drift[0].detail

    def test_legacy_expected_layout(self, tmp_path: Path) -> None:
        """Mined tasks ship ground_truth.json at task root with `expected`."""
        task = self._make_dual_task(
            tmp_path,
            gt_payload={
                "oracle_type": "file_list",
                "expected": ["pkg/a.go", "pkg/b.go"],
            },
            gt_in_tests_dir=False,
            verification_mode="test_script",
        )
        (task / "answer.txt").write_text("pkg/a.go\npkg/b.go\n")
        results = run_validate(task)
        drift = self._drift_results(results)
        assert len(drift) == 1
        assert "matches ground_truth (2 items)" in drift[0].detail

    def test_legacy_expected_drift(self, tmp_path: Path) -> None:
        task = self._make_dual_task(
            tmp_path,
            gt_payload={
                "oracle_type": "file_list",
                "expected": ["pkg/a.go", "pkg/b.go", "pkg/c.go"],
            },
            gt_in_tests_dir=False,
            verification_mode="test_script",
        )
        (task / "answer.txt").write_text("pkg/a.go\npkg/c.go\npkg/d.go\n")
        results = run_validate(task)
        drift = self._drift_results(results)
        assert len(drift) == 1
        assert drift[0].detail.startswith("warn:")

    def test_count_match(self, tmp_path: Path) -> None:
        task = self._make_dual_task(
            tmp_path,
            gt_payload={"answer_type": "count", "answer": 7},
        )
        (task / "answer.txt").write_text("7\n")
        results = run_validate(task)
        drift = self._drift_results(results)
        assert len(drift) == 1
        assert "matches ground_truth (count=7)" in drift[0].detail

    def test_count_drift(self, tmp_path: Path) -> None:
        task = self._make_dual_task(
            tmp_path,
            gt_payload={"answer_type": "count", "answer": 7},
        )
        (task / "answer.txt").write_text("9\n")
        results = run_validate(task)
        drift = self._drift_results(results)
        assert len(drift) == 1
        assert drift[0].detail.startswith("warn:")
        assert "answer.txt=9" in drift[0].detail
        assert "ground_truth=7" in drift[0].detail

    def test_boolean_match(self, tmp_path: Path) -> None:
        task = self._make_dual_task(
            tmp_path,
            gt_payload={"answer_type": "boolean", "answer": True},
        )
        (task / "answer.txt").write_text("true\n")
        results = run_validate(task)
        drift = self._drift_results(results)
        assert len(drift) == 1
        assert "matches ground_truth (boolean=True)" in drift[0].detail

    def test_boolean_drift(self, tmp_path: Path) -> None:
        task = self._make_dual_task(
            tmp_path,
            gt_payload={"answer_type": "boolean", "answer": True},
        )
        (task / "answer.txt").write_text("no\n")
        results = run_validate(task)
        drift = self._drift_results(results)
        assert len(drift) == 1
        assert drift[0].detail.startswith("warn:")

    def test_scalar_string_match(self, tmp_path: Path) -> None:
        task = self._make_dual_task(
            tmp_path,
            gt_payload={"answer_type": "string", "answer": "hello world"},
        )
        (task / "answer.txt").write_text("hello world\n")
        results = run_validate(task)
        drift = self._drift_results(results)
        assert len(drift) == 1
        assert "matches ground_truth (scalar)" in drift[0].detail

    def test_scalar_string_drift(self, tmp_path: Path) -> None:
        task = self._make_dual_task(
            tmp_path,
            gt_payload={"answer_type": "string", "answer": "expected"},
        )
        (task / "answer.txt").write_text("something else\n")
        results = run_validate(task)
        drift = self._drift_results(results)
        assert len(drift) == 1
        assert drift[0].detail.startswith("warn:")

    def test_v2_checks_layout(self, tmp_path: Path) -> None:
        """v2 schema uses a `checks` array; pick the first answerable entry."""
        task = self._make_dual_task(
            tmp_path,
            gt_payload={
                "checks": [
                    {"answer_type": "file_list", "answer": ["x/a.py", "x/b.py"]},
                ],
            },
        )
        (task / "answer.txt").write_text("x/a.py\nx/b.py\n")
        results = run_validate(task)
        drift = self._drift_results(results)
        assert len(drift) == 1
        assert "matches ground_truth (2 items)" in drift[0].detail

    def test_drift_does_not_fail_validate(self, tmp_path: Path) -> None:
        """Drift is advisory: validate must still exit 0 in CLI mode."""
        task = self._make_dual_task(
            tmp_path,
            gt_payload={"answer_type": "count", "answer": 1},
        )
        (task / "answer.txt").write_text("99\n")
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(task)])
        assert result.exit_code == 0, result.output
        assert "warn: answer.txt disagrees" in result.output

    def test_comments_and_blanks_ignored_in_file_list(self, tmp_path: Path) -> None:
        task = self._make_dual_task(
            tmp_path,
            gt_payload={
                "answer_type": "file_list",
                "answer": ["src/a.py", "src/b.py"],
            },
        )
        (task / "answer.txt").write_text(
            "# header comment\n"
            "src/a.py\n"
            "\n"
            "src/b.py\n"
            "# trailing\n"
        )
        results = run_validate(task)
        drift = self._drift_results(results)
        assert len(drift) == 1
        assert "matches ground_truth" in drift[0].detail

    def test_no_comparable_answer_skipped(self, tmp_path: Path) -> None:
        """ground_truth without answer/expected/checks emits an info skip."""
        task = self._make_dual_task(
            tmp_path,
            gt_payload={"answer_type": "weird_custom"},
        )
        (task / "answer.txt").write_text("anything\n")
        results = run_validate(task)
        drift = self._drift_results(results)
        assert len(drift) == 1
        assert drift[0].detail.startswith("info:")
        assert "no comparable" in drift[0].detail


class TestValidateCLI:
    """Integration tests for the CLI command."""

    def test_valid_task_exits_zero(self, valid_task_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(valid_task_dir)])
        assert result.exit_code == 0
        assert "PASS" in result.output
        assert "FAIL" not in result.output

    def test_missing_instruction_exits_one(self, tmp_path: Path) -> None:
        (tmp_path / "task.toml").write_text('[metadata]\nname = "x"\n')
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 1
        assert "instruction.md" in result.output

    def test_bad_json_exits_one(self, tmp_path: Path) -> None:
        (tmp_path / "instruction.md").write_text("# Task\n")
        (tmp_path / "metadata.json").write_text("{bad")
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 1
        assert "parse error" in result.output

    def test_strict_flag_prints_placeholder(self, valid_task_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["validate", "--strict", str(valid_task_dir)])
        assert "LLM spot-check not yet implemented" in result.output

    def test_artifact_eval_valid(self, artifact_eval_task_dir: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(artifact_eval_task_dir)])
        assert result.exit_code == 0
        assert "PASS" in result.output

    def test_nonexistent_dir(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["validate", "/nonexistent/path"])
        assert result.exit_code != 0


class TestValidateMultiTaskDir:
    """`codeprobe validate <parent>` should iterate over child task dirs."""

    def _make_valid_task(self, parent: Path, name: str) -> Path:
        d = parent / name
        d.mkdir()
        (d / "instruction.md").write_text("# Task\nDo a thing.\n")
        (d / "task.toml").write_text(
            '[metadata]\nname = "t"\ntask_type = "sdlc_code_change"\n\n'
            '[verification]\nverification_mode = "test_script"\n'
        )
        tests_dir = d / "tests"
        tests_dir.mkdir()
        ts = tests_dir / "test.sh"
        ts.write_text("#!/bin/bash\nexit 0\n")
        ts.chmod(ts.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return d

    def test_parent_dir_with_all_valid_tasks_exits_zero(self, tmp_path: Path) -> None:
        self._make_valid_task(tmp_path, "task-a")
        self._make_valid_task(tmp_path, "task-b")

        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "PASS  task-a" in result.output
        assert "PASS  task-b" in result.output
        assert "Validated 2 task(s): 2 passed, 0 failed." in result.output

    def test_parent_dir_with_one_broken_task_exits_one(self, tmp_path: Path) -> None:
        self._make_valid_task(tmp_path, "good")
        bad = tmp_path / "bad"
        bad.mkdir()
        (bad / "instruction.md").write_text("# Bad\n")
        # Missing task.toml / metadata.json → metadata check fails

        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "PASS  good" in result.output
        assert "FAIL  bad" in result.output
        assert "Validated 2 task(s): 1 passed, 1 failed." in result.output

    def test_single_task_dir_still_works(self, valid_task_dir: Path) -> None:
        """Passing a single task directly keeps legacy per-check output."""
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(valid_task_dir)])
        assert result.exit_code == 0, result.output
        # Per-check markers ("PASS  instruction.md exists ...") are the
        # legacy single-task output; the multi-task summary line should
        # NOT appear here.
        assert "Validated" not in result.output

    def test_parent_with_non_task_children_falls_back(self, tmp_path: Path) -> None:
        """If children don't look like tasks, fall through to legacy mode."""
        (tmp_path / "random-dir").mkdir()
        (tmp_path / "README.md").write_text("just a readme\n")

        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        # No task-shape anywhere → legacy single-task mode runs and fails
        assert result.exit_code == 1
        assert "Validated" not in result.output

    def test_discovers_nested_task_subdirectories(self, tmp_path: Path) -> None:
        """Tasks nested below the top level are discovered recursively.

        Regression for BUG-VALIDATE-DISCOVERY-005: a ``group/task-001``
        layout must surface ``task-001`` even though the task sits two levels
        below the argument. One-level (``iterdir``) discovery missed it.
        """
        group = tmp_path / "group-a"
        group.mkdir()
        self._make_valid_task(group, "task-001")

        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "task-001" in result.output
        assert "Validated 1 task(s): 1 passed, 0 failed." in result.output

    def test_does_not_descend_into_a_task_own_subdirs(self, tmp_path: Path) -> None:
        """A task's own ``tests/`` subtree is not mistaken for a nested task."""
        # One task under the parent; the task carries a tests/ subdir. Recursive
        # discovery must stop at the task and not walk into tests/, so the count
        # stays exactly 1.
        self._make_valid_task(tmp_path, "task-001")
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "Validated 1 task(s): 1 passed, 0 failed." in result.output

    def test_discovery_handles_pathologically_deep_nesting(
        self, tmp_path: Path
    ) -> None:
        """A very deep non-task chain must not raise RecursionError.

        Recursive discovery blew the interpreter stack past ~1000 levels; the
        iterative walker must find the leaf task at depth > 1100 and return it.
        """
        from codeprobe.cli.validate_cmd import _find_task_dirs

        chain: list[Path] = []
        cur = tmp_path
        for _ in range(1200):
            cur = cur / "d"
            cur.mkdir()
            chain.append(cur)
        task = self._make_valid_task(cur, "task-001")

        try:
            found = _find_task_dirs(tmp_path)
            assert [p.name for p in found] == ["task-001"]
        finally:
            # Tear the deep chain down iteratively: pytest's tmp_path cleanup
            # (shutil.rmtree) and os.walk both recurse, so they would hit the
            # same RecursionError on this tree. rmdir bottom-up avoids it.
            shutil.rmtree(task)  # task subtree is shallow
            for d in reversed(chain):
                d.rmdir()

    def test_discovery_propagates_io_errors_not_silent_partial(
        self, tmp_path: Path
    ) -> None:
        """An unlistable subdirectory must surface as an error, not be silently
        skipped — a swallowed OSError makes a partial discovery look complete.

        Mode ``0o111`` (execute, no read) is the exact case the swallow hid:
        ``_looks_like_task_dir`` can still stat (absent) children and returns
        False, so the walk descends and ``iterdir()`` raises PermissionError.
        """
        import os

        from codeprobe.cli.validate_cmd import _find_task_dirs

        if os.geteuid() == 0:
            pytest.skip("root bypasses directory permissions")
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o111)
        try:
            with pytest.raises(OSError):
                _find_task_dirs(tmp_path)
        finally:
            locked.chmod(0o755)

    def test_nested_tasks_fixture_surfaces_task_001(self) -> None:
        """The committed ``nested_tasks`` fixture — the acceptance fixture for
        BUG-VALIDATE-DISCOVERY-005 — must exist and surface ``task-001``.

        Exit code 0 is asserted alongside the substring: the criterion's
        ``cli_stdout_contains`` check inspects only stdout, so a fixture with
        an invalid task (validate exit 1) that still printed ``task-001``
        would green vacuously. Every discovered task must validate cleanly.
        """
        fixture = Path(__file__).resolve().parent / "fixtures" / "nested_tasks"
        assert fixture.is_dir(), f"missing acceptance fixture: {fixture}"
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(fixture)])
        assert result.exit_code == 0, result.output
        assert "task-001" in result.output, result.output
        assert "task-002" in result.output, result.output
