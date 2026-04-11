"""Tests for ground_truth.json schema enforcement across validate + ArtifactScorer.

Covers three adversarial holes:
1. Payload shape mismatch (file_list with bare string instead of list)
2. answer_type cross-check between ground_truth and agent answer
3. Deserialization size limit in _load_json_file
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.core.scoring import (
    ArtifactScorer,
    _load_json_file,
    score_count,
    score_file_list,
    validate_ground_truth,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_task(
    tmp_path: Path,
    gt: dict,
    answer: dict,
) -> Path:
    """Build a minimal task directory with ground_truth.json and answer.json."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    _write_json(tests_dir / "ground_truth.json", gt)
    _write_json(task_dir / "answer.json", answer)
    return task_dir


# ===========================================================================
# 1. Payload shape mismatch — score_file_list with bare string
# ===========================================================================


class TestScoreFileListBareString:
    """score_file_list must return an explicit error when expected is a bare string."""

    def test_bare_string_expected_returns_error(self) -> None:
        result = score_file_list("src/foo.py", ["src/foo.py"])
        assert result.score == 0.0
        assert result.passed is False
        assert result.error is not None
        assert "expected" in result.error.lower() or "list" in result.error.lower()

    def test_bare_string_actual_returns_error(self) -> None:
        result = score_file_list(["src/foo.py"], "src/foo.py")
        assert result.score == 0.0
        assert result.passed is False
        assert result.error is not None
        assert "list" in result.error.lower()


# ===========================================================================
# 2. score_count with non-int-convertible expected
# ===========================================================================


class TestScoreCountValidation:
    """score_count must return an error when expected is not int-convertible."""

    def test_list_expected_returns_error(self) -> None:
        result = score_count(["a", "b"], 2)
        assert result.score == 0.0
        assert result.passed is False
        assert result.error is not None

    def test_none_expected_returns_error(self) -> None:
        result = score_count(None, 5)
        assert result.score == 0.0
        assert result.passed is False
        assert result.error is not None


# ===========================================================================
# 3. ArtifactScorer end-to-end: bare string file_list
# ===========================================================================


class TestArtifactScorerBareStringFileList:
    """ArtifactScorer with file_list ground truth that has a bare string answer."""

    def test_bare_string_gt_answer_scores_zero_with_error(self, tmp_path: Path) -> None:
        gt = {"answer_type": "file_list", "answer": "src/foo.py"}
        answer = {"answer": ["src/foo.py"]}
        task_dir = _make_task(tmp_path, gt, answer)

        result = ArtifactScorer().score("", task_dir)
        assert result.score == 0.0
        assert result.passed is False
        assert result.error is not None
        assert "list" in result.error.lower()


# ===========================================================================
# 4. answer_type cross-check warning
# ===========================================================================


class TestAnswerTypeCrossCheck:
    """ArtifactScorer should warn when answer_data.answer_type != gt.answer_type."""

    def test_mismatched_answer_type_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        gt = {"answer_type": "count", "answer": 5}
        answer = {"answer_type": "text", "answer": 5}
        task_dir = _make_task(tmp_path, gt, answer)

        with caplog.at_level("WARNING", logger="codeprobe.core.scoring"):
            ArtifactScorer().score("", task_dir)

        # Should still score (non-fatal), but log a warning
        assert any("mismatch" in r.message.lower() for r in caplog.records)


# ===========================================================================
# 5. _load_json_file size guard
# ===========================================================================


class TestLoadJsonFileSizeGuard:
    """_load_json_file must reject files exceeding _MAX_GROUND_TRUTH_BYTES."""

    def test_oversized_file_returns_none(self, tmp_path: Path) -> None:
        big_file = tmp_path / "big.json"
        # Write 11 MB of valid JSON
        big_file.write_text(
            '{"x": "' + "a" * (11 * 1024 * 1024) + '"}', encoding="utf-8"
        )
        result = _load_json_file(big_file)
        assert result is None

    def test_normal_file_loads_ok(self, tmp_path: Path) -> None:
        f = tmp_path / "small.json"
        _write_json(f, {"answer_type": "count", "answer": 42})
        result = _load_json_file(f)
        assert result == {"answer_type": "count", "answer": 42}


# ===========================================================================
# 6. validate_ground_truth v1 shape validation
# ===========================================================================


class TestValidateGroundTruthV1Shapes:
    """validate_ground_truth must reject v1 shape mismatches."""

    def test_file_list_with_string_answer(self) -> None:
        gt = {"answer_type": "file_list", "answer": "src/foo.py"}
        err = validate_ground_truth(gt)
        assert err is not None
        assert "list" in err.lower()

    def test_file_list_with_list_answer(self) -> None:
        gt = {"answer_type": "file_list", "answer": ["src/foo.py"]}
        err = validate_ground_truth(gt)
        assert err is None

    def test_count_with_list_answer(self) -> None:
        gt = {"answer_type": "count", "answer": [1, 2]}
        err = validate_ground_truth(gt)
        assert err is not None
        assert "int" in err.lower()

    def test_count_with_int_answer(self) -> None:
        gt = {"answer_type": "count", "answer": 5}
        err = validate_ground_truth(gt)
        assert err is None

    def test_count_with_string_int_answer(self) -> None:
        gt = {"answer_type": "count", "answer": "5"}
        err = validate_ground_truth(gt)
        assert err is None

    def test_boolean_with_list_answer(self) -> None:
        gt = {"answer_type": "boolean", "answer": [True]}
        err = validate_ground_truth(gt)
        assert err is not None

    def test_boolean_with_bool_answer(self) -> None:
        gt = {"answer_type": "boolean", "answer": True}
        err = validate_ground_truth(gt)
        assert err is None

    def test_boolean_with_string_answer(self) -> None:
        gt = {"answer_type": "boolean", "answer": "true"}
        err = validate_ground_truth(gt)
        assert err is None

    def test_text_with_string_answer(self) -> None:
        gt = {"answer_type": "text", "answer": "hello"}
        err = validate_ground_truth(gt)
        assert err is None

    def test_text_with_list_answer(self) -> None:
        gt = {"answer_type": "text", "answer": ["hello"]}
        err = validate_ground_truth(gt)
        assert err is not None

    def test_symbol_list_with_string_answer(self) -> None:
        gt = {"answer_type": "symbol_list", "answer": "MyClass"}
        err = validate_ground_truth(gt)
        assert err is not None
        assert "list" in err.lower()

    def test_dependency_chain_with_string_answer(self) -> None:
        gt = {"answer_type": "dependency_chain", "answer": "foo"}
        err = validate_ground_truth(gt)
        assert err is not None
        assert "list" in err.lower()


# ===========================================================================
# 7. validate_cmd _check_ground_truth_dual rejects bad shapes
# ===========================================================================


class TestCheckGroundTruthDualShapeValidation:
    """_check_ground_truth_dual should reject invalid v1 shapes."""

    def test_file_list_with_string_fails(self, tmp_path: Path) -> None:
        from codeprobe.cli.validate_cmd import _check_ground_truth_dual

        task_dir = tmp_path / "task"
        tests_dir = task_dir / "tests"
        tests_dir.mkdir(parents=True)
        _write_json(
            tests_dir / "ground_truth.json",
            {"answer_type": "file_list", "answer": "src/foo.py"},
        )

        result = _check_ground_truth_dual(task_dir)
        assert result.passed is False
        assert "list" in result.detail.lower()

    def test_valid_file_list_passes(self, tmp_path: Path) -> None:
        from codeprobe.cli.validate_cmd import _check_ground_truth_dual

        task_dir = tmp_path / "task"
        tests_dir = task_dir / "tests"
        tests_dir.mkdir(parents=True)
        _write_json(
            tests_dir / "ground_truth.json",
            {"answer_type": "file_list", "answer": ["src/foo.py"]},
        )

        result = _check_ground_truth_dual(task_dir)
        assert result.passed is True

    def test_oversized_ground_truth_fails(self, tmp_path: Path) -> None:
        from codeprobe.cli.validate_cmd import _check_ground_truth_dual

        task_dir = tmp_path / "task"
        tests_dir = task_dir / "tests"
        tests_dir.mkdir(parents=True)
        gt_path = tests_dir / "ground_truth.json"
        # Write 11 MB
        gt_path.write_text(
            '{"answer_type": "count", "answer": "' + "x" * (11 * 1024 * 1024) + '"}',
            encoding="utf-8",
        )

        result = _check_ground_truth_dual(task_dir)
        assert result.passed is False
        assert "size" in result.detail.lower() or "large" in result.detail.lower()
