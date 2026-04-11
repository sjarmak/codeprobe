"""Tests for the validate command's dual-mode and scoring_policy checks.

Covers work unit u9-validate-dual:
- dual verification_mode requires BOTH tests/test.sh (executable) AND
  tests/ground_truth.json (with 'answer' field, new schema)
- scoring_policy must be one of {'', 'min', 'mean', 'weighted'}
- for 'weighted', weight_direct + weight_artifact must equal 1.0 (+/- 1e-6)
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.cli.validate_cmd import run_validate


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_instruction(task_dir: Path) -> None:
    (task_dir / "instruction.md").write_text("# Task\nDual mode task.\n")


def _write_test_sh(task_dir: Path, *, executable: bool = True) -> Path:
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    test_sh = tests_dir / "test.sh"
    test_sh.write_text("#!/bin/bash\nexit 0\n")
    if executable:
        _make_executable(test_sh)
    else:
        # Explicitly strip execute bits
        test_sh.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return test_sh


def _write_ground_truth(task_dir: Path, payload: dict) -> Path:
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(exist_ok=True)
    gt = tests_dir / "ground_truth.json"
    gt.write_text(json.dumps(payload))
    return gt


def _write_task_toml(
    task_dir: Path,
    *,
    verification_mode: str = "dual",
    scoring_policy: str | None = None,
    weight_direct: float | None = None,
    weight_artifact: float | None = None,
) -> None:
    lines = [
        "[metadata]",
        'name = "dual-task"',
        'task_type = "sdlc_code_change"',
        "",
        "[verification]",
        f'verification_mode = "{verification_mode}"',
    ]
    if scoring_policy is not None:
        lines.append(f'scoring_policy = "{scoring_policy}"')
    if weight_direct is not None:
        lines.append(f"weight_direct = {weight_direct}")
    if weight_artifact is not None:
        lines.append(f"weight_artifact = {weight_artifact}")
    (task_dir / "task.toml").write_text("\n".join(lines) + "\n")


@pytest.fixture()
def dual_complete(tmp_path: Path) -> Path:
    """A fully valid dual-mode task directory."""
    _write_instruction(tmp_path)
    _write_task_toml(tmp_path, verification_mode="dual")
    _write_test_sh(tmp_path, executable=True)
    _write_ground_truth(tmp_path, {"answer_type": "boolean", "answer": True})
    return tmp_path


# ---------------------------------------------------------------------------
# Dual-mode structural checks
# ---------------------------------------------------------------------------


class TestDualMode:
    def test_dual_complete_passes(self, dual_complete: Path) -> None:
        results = run_validate(dual_complete)
        failures = [r for r in results if not r.passed]
        assert not failures, failures

    def test_dual_missing_ground_truth(self, tmp_path: Path) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(tmp_path, verification_mode="dual")
        _write_test_sh(tmp_path, executable=True)
        # No ground_truth.json
        results = run_validate(tmp_path)
        gt = next(r for r in results if "ground_truth" in r.name)
        assert not gt.passed
        assert "ground_truth.json" in gt.detail

    def test_dual_missing_test_sh(self, tmp_path: Path) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(tmp_path, verification_mode="dual")
        # Create tests/ dir and ground_truth.json but no test.sh
        _write_ground_truth(tmp_path, {"answer": True})
        results = run_validate(tmp_path)
        ts = next(r for r in results if "test.sh" in r.name)
        assert not ts.passed
        assert "test.sh" in ts.detail

    def test_dual_test_sh_not_executable(self, tmp_path: Path) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(tmp_path, verification_mode="dual")
        _write_test_sh(tmp_path, executable=False)
        _write_ground_truth(tmp_path, {"answer": "foo"})
        results = run_validate(tmp_path)
        ts = next(r for r in results if "test.sh" in r.name)
        assert not ts.passed
        assert "not executable" in ts.detail

    def test_dual_ground_truth_missing_answer_field(self, tmp_path: Path) -> None:
        """Dual mode requires the new schema with 'answer' key."""
        _write_instruction(tmp_path)
        _write_task_toml(tmp_path, verification_mode="dual")
        _write_test_sh(tmp_path, executable=True)
        # Old-format ground truth — has answer_type but not 'answer'
        _write_ground_truth(tmp_path, {"answer_type": "boolean", "expected": True})
        results = run_validate(tmp_path)
        gt = next(r for r in results if "ground_truth" in r.name)
        assert not gt.passed
        assert "answer" in gt.detail

    def test_dual_ground_truth_bad_json(self, tmp_path: Path) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(tmp_path, verification_mode="dual")
        _write_test_sh(tmp_path, executable=True)
        tests_dir = tmp_path / "tests"
        (tests_dir / "ground_truth.json").write_text("{not valid json")
        results = run_validate(tmp_path)
        gt = next(r for r in results if "ground_truth" in r.name)
        assert not gt.passed
        assert "parse error" in gt.detail

    def test_dual_ground_truth_not_object(self, tmp_path: Path) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(tmp_path, verification_mode="dual")
        _write_test_sh(tmp_path, executable=True)
        tests_dir = tmp_path / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / "ground_truth.json").write_text(
            json.dumps(["not", "an", "object"])
        )
        results = run_validate(tmp_path)
        gt = next(r for r in results if "ground_truth" in r.name)
        assert not gt.passed


# ---------------------------------------------------------------------------
# scoring_policy validation
# ---------------------------------------------------------------------------


class TestScoringPolicy:
    def test_valid_empty_policy(self, dual_complete: Path) -> None:
        # dual_complete omits scoring_policy, which means default "" — valid.
        results = run_validate(dual_complete)
        assert all(r.passed for r in results), [r for r in results if not r.passed]

    @pytest.mark.parametrize("policy", ["min", "mean"])
    def test_valid_non_weighted_policies(self, tmp_path: Path, policy: str) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(tmp_path, verification_mode="dual", scoring_policy=policy)
        _write_test_sh(tmp_path, executable=True)
        _write_ground_truth(tmp_path, {"answer": True})
        results = run_validate(tmp_path)
        sp = next(r for r in results if "scoring_policy" in r.name)
        assert sp.passed
        # No weight_sum check expected for non-weighted policies
        assert not any("weight sum" in r.name for r in results)

    def test_invalid_policy_garbage(self, tmp_path: Path) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(tmp_path, verification_mode="dual", scoring_policy="garbage")
        _write_test_sh(tmp_path, executable=True)
        _write_ground_truth(tmp_path, {"answer": True})
        results = run_validate(tmp_path)
        sp = next(r for r in results if "scoring_policy" in r.name)
        assert not sp.passed
        assert "garbage" in sp.detail

    def test_weighted_policy_bad_sum(self, tmp_path: Path) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(
            tmp_path,
            verification_mode="dual",
            scoring_policy="weighted",
            weight_direct=0.4,
            weight_artifact=0.4,
        )
        _write_test_sh(tmp_path, executable=True)
        _write_ground_truth(tmp_path, {"answer": True})
        results = run_validate(tmp_path)
        ws = next(r for r in results if "weight sum" in r.name)
        assert not ws.passed
        assert "0.8" in ws.detail or "expected 1.0" in ws.detail

    def test_weighted_policy_good_sum(self, tmp_path: Path) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(
            tmp_path,
            verification_mode="dual",
            scoring_policy="weighted",
            weight_direct=0.3,
            weight_artifact=0.7,
        )
        _write_test_sh(tmp_path, executable=True)
        _write_ground_truth(tmp_path, {"answer": True})
        results = run_validate(tmp_path)
        failures = [r for r in results if not r.passed]
        assert not failures, failures

    def test_weighted_policy_floating_tolerance(self, tmp_path: Path) -> None:
        """A sum that's within 1e-6 of 1.0 should pass."""
        _write_instruction(tmp_path)
        _write_task_toml(
            tmp_path,
            verification_mode="dual",
            scoring_policy="weighted",
            weight_direct=0.1 + 0.2,  # floating-point fun: 0.30000000000000004
            weight_artifact=0.7,
        )
        _write_test_sh(tmp_path, executable=True)
        _write_ground_truth(tmp_path, {"answer": True})
        results = run_validate(tmp_path)
        ws = next(r for r in results if "weight sum" in r.name)
        assert ws.passed


# ---------------------------------------------------------------------------
# CLI exit code behaviour
# ---------------------------------------------------------------------------


class TestCLIExitCodes:
    def test_dual_complete_exits_zero(self, dual_complete: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(dual_complete)])
        assert result.exit_code == 0, result.output
        assert "PASS" in result.output
        assert "FAIL" not in result.output

    def test_dual_missing_ground_truth_exits_one(self, tmp_path: Path) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(tmp_path, verification_mode="dual")
        _write_test_sh(tmp_path, executable=True)
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 1
        assert "ground_truth.json" in result.output

    def test_dual_missing_test_sh_exits_one(self, tmp_path: Path) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(tmp_path, verification_mode="dual")
        _write_ground_truth(tmp_path, {"answer": True})
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 1
        assert "test.sh" in result.output

    def test_dual_test_sh_not_executable_exits_one(self, tmp_path: Path) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(tmp_path, verification_mode="dual")
        _write_test_sh(tmp_path, executable=False)
        _write_ground_truth(tmp_path, {"answer": True})
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 1
        assert "not executable" in result.output

    def test_scoring_policy_garbage_exits_one(self, tmp_path: Path) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(tmp_path, verification_mode="dual", scoring_policy="garbage")
        _write_test_sh(tmp_path, executable=True)
        _write_ground_truth(tmp_path, {"answer": True})
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 1
        assert "garbage" in result.output

    def test_weighted_bad_sum_exits_one(self, tmp_path: Path) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(
            tmp_path,
            verification_mode="dual",
            scoring_policy="weighted",
            weight_direct=0.4,
            weight_artifact=0.4,
        )
        _write_test_sh(tmp_path, executable=True)
        _write_ground_truth(tmp_path, {"answer": True})
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 1
        assert "weight" in result.output.lower()

    def test_weighted_good_sum_exits_zero(self, tmp_path: Path) -> None:
        _write_instruction(tmp_path)
        _write_task_toml(
            tmp_path,
            verification_mode="dual",
            scoring_policy="weighted",
            weight_direct=0.3,
            weight_artifact=0.7,
        )
        _write_test_sh(tmp_path, executable=True)
        _write_ground_truth(tmp_path, {"answer": True})
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "FAIL" not in result.output


# ---------------------------------------------------------------------------
# Strict weight validation — invalid weight values must cause failure
# ---------------------------------------------------------------------------


class TestStrictWeightValidation:
    """validate_cmd must reject invalid weight values instead of silently coercing."""

    def test_weight_direct_non_numeric_exits_one(self, tmp_path: Path) -> None:
        """weight_direct = 'abc' must be rejected."""
        _write_instruction(tmp_path)
        _write_task_toml(
            tmp_path,
            verification_mode="dual",
            scoring_policy="weighted",
            weight_direct=0.5,  # will be overwritten below
            weight_artifact=0.5,
        )
        # Overwrite task.toml with raw string weight
        lines = [
            "[metadata]",
            'name = "dual-task"',
            'task_type = "sdlc_code_change"',
            "",
            "[verification]",
            'verification_mode = "dual"',
            'scoring_policy = "weighted"',
            'weight_direct = "abc"',
            "weight_artifact = 0.5",
        ]
        (tmp_path / "task.toml").write_text("\n".join(lines) + "\n")
        _write_test_sh(tmp_path, executable=True)
        _write_ground_truth(tmp_path, {"answer": True})
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "weight" in result.output.lower()

    def test_weight_negative_exits_one(self, tmp_path: Path) -> None:
        """weight_direct = -0.1 must be rejected."""
        _write_instruction(tmp_path)
        lines = [
            "[metadata]",
            'name = "dual-task"',
            'task_type = "sdlc_code_change"',
            "",
            "[verification]",
            'verification_mode = "dual"',
            'scoring_policy = "weighted"',
            "weight_direct = -0.1",
            "weight_artifact = 1.1",
        ]
        (tmp_path / "task.toml").write_text("\n".join(lines) + "\n")
        _write_test_sh(tmp_path, executable=True)
        _write_ground_truth(tmp_path, {"answer": True})
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "weight" in result.output.lower()

    def test_weight_infinity_exits_one(self, tmp_path: Path) -> None:
        """weight_direct = inf must be rejected."""
        _write_instruction(tmp_path)
        lines = [
            "[metadata]",
            'name = "dual-task"',
            'task_type = "sdlc_code_change"',
            "",
            "[verification]",
            'verification_mode = "dual"',
            'scoring_policy = "weighted"',
            "weight_direct = inf",
            "weight_artifact = 0.5",
        ]
        (tmp_path / "task.toml").write_text("\n".join(lines) + "\n")
        _write_test_sh(tmp_path, executable=True)
        _write_ground_truth(tmp_path, {"answer": True})
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 1, result.output
        assert "weight" in result.output.lower()

    def test_valid_weights_exits_zero(self, tmp_path: Path) -> None:
        """Valid weight_direct=0.3, weight_artifact=0.7 passes."""
        _write_instruction(tmp_path)
        _write_task_toml(
            tmp_path,
            verification_mode="dual",
            scoring_policy="weighted",
            weight_direct=0.3,
            weight_artifact=0.7,
        )
        _write_test_sh(tmp_path, executable=True)
        _write_ground_truth(tmp_path, {"answer": True})
        runner = CliRunner()
        result = runner.invoke(main, ["validate", str(tmp_path)])
        assert result.exit_code == 0, result.output
