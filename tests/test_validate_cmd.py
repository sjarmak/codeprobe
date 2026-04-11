"""Tests for the validate command."""

from __future__ import annotations

import json
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
