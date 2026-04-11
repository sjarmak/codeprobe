"""Tests for ground_truth.json v2 schema — multi-check scoring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.core.scoring import ArtifactScorer, ScoreResult


def _write_json(path: Path, data: dict | list) -> None:
    """Write JSON data to a file, creating parent dirs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_artifact_task(
    tmp_path: Path,
    ground_truth: dict,
    answer: dict,
    *,
    name: str = "task",
) -> Path:
    """Create a task directory with ground_truth.json and answer.json."""
    task_dir = tmp_path / name
    task_dir.mkdir(parents=True, exist_ok=True)
    _write_json(task_dir / "tests" / "ground_truth.json", ground_truth)
    _write_json(task_dir / "answer.json", answer)
    return task_dir


# ---------------------------------------------------------------------------
# V2 checks array scoring
# ---------------------------------------------------------------------------


class TestV2ChecksScoring:
    """Test multi-check weighted composite scoring."""

    def test_two_checks_weighted_composite(self, tmp_path: Path) -> None:
        """file_list (0.6) + count (0.4), both pass → composite near 1.0."""
        gt = {
            "checks": [
                {"answer_type": "file_list", "answer": ["src/foo.py"], "weight": 0.6},
                {"answer_type": "count", "answer": 3, "weight": 0.4},
            ],
        }
        answer = {
            "answers": [
                {"answer_type": "file_list", "answer": ["src/foo.py"]},
                {"answer_type": "count", "answer": 3},
            ],
        }
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="two-checks")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_single_check_weight_1(self, tmp_path: Path) -> None:
        """Single check with weight 1.0 → score equals that check."""
        gt = {
            "checks": [
                {"answer_type": "count", "answer": 5, "weight": 1.0},
            ],
        }
        answer = {
            "answers": [
                {"answer_type": "count", "answer": 5},
            ],
        }
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="single-check")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_all_checks_pass(self, tmp_path: Path) -> None:
        """All checks pass → composite near 1.0."""
        gt = {
            "checks": [
                {"answer_type": "boolean", "answer": "true", "weight": 0.5},
                {"answer_type": "text", "answer": "hello", "weight": 0.5},
            ],
        }
        answer = {
            "answers": [
                {"answer_type": "boolean", "answer": "true"},
                {"answer_type": "text", "answer": "hello"},
            ],
        }
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="all-pass")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_all_checks_fail(self, tmp_path: Path) -> None:
        """All checks fail → composite 0.0."""
        gt = {
            "checks": [
                {"answer_type": "count", "answer": 5, "weight": 0.6},
                {"answer_type": "boolean", "answer": "true", "weight": 0.4},
            ],
        }
        answer = {
            "answers": [
                {"answer_type": "count", "answer": 99},
                {"answer_type": "boolean", "answer": "false"},
            ],
        }
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="all-fail")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == pytest.approx(0.0)
        assert result.passed is False

    def test_mixed_pass_fail_weighted(self, tmp_path: Path) -> None:
        """count passes (0.4), file_list fails (0.6) → composite = 0.4."""
        gt = {
            "checks": [
                {"answer_type": "file_list", "answer": ["a.py"], "weight": 0.6},
                {"answer_type": "count", "answer": 3, "weight": 0.4},
            ],
        }
        answer = {
            "answers": [
                {"answer_type": "file_list", "answer": ["wrong.py"]},
                {"answer_type": "count", "answer": 3},
            ],
        }
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="mixed")
        result = ArtifactScorer().score("", task_dir)
        # file_list F1 = 0.0 (no overlap), count = 1.0
        # composite = 0.6 * 0.0 + 0.4 * 1.0 = 0.4
        assert result.score == pytest.approx(0.4)
        assert result.passed is False  # 0.4 < PASS_THRESHOLD (0.5)

    def test_details_contain_per_check_breakdown(self, tmp_path: Path) -> None:
        """Result details should contain per-check scores."""
        gt = {
            "checks": [
                {"answer_type": "count", "answer": 1, "weight": 0.5},
                {"answer_type": "boolean", "answer": "true", "weight": 0.5},
            ],
        }
        answer = {
            "answers": [
                {"answer_type": "count", "answer": 1},
                {"answer_type": "boolean", "answer": "false"},
            ],
        }
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="details")
        result = ArtifactScorer().score("", task_dir)
        assert "check_scores" in result.details
        assert len(result.details["check_scores"]) == 2


# ---------------------------------------------------------------------------
# Weight validation
# ---------------------------------------------------------------------------


class TestV2WeightValidation:
    """Weights must sum to 1.0 within tolerance."""

    def test_weights_must_sum_to_one(self, tmp_path: Path) -> None:
        gt = {
            "checks": [
                {"answer_type": "count", "answer": 1, "weight": 0.3},
                {"answer_type": "boolean", "answer": "true", "weight": 0.3},
            ],
        }
        answer = {"answers": []}
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="bad-sum")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == 0.0
        assert result.passed is False
        assert result.error is not None
        assert "weight" in result.error.lower()

    def test_weights_sum_within_tolerance(self, tmp_path: Path) -> None:
        """Weights summing to ~1.0 within 1e-6 should be accepted."""
        gt = {
            "checks": [
                {"answer_type": "count", "answer": 1, "weight": 1.0 / 3},
                {"answer_type": "boolean", "answer": "true", "weight": 1.0 / 3},
                {"answer_type": "text", "answer": "hi", "weight": 1.0 / 3},
            ],
        }
        answer = {
            "answers": [
                {"answer_type": "count", "answer": 1},
                {"answer_type": "boolean", "answer": "true"},
                {"answer_type": "text", "answer": "hi"},
            ],
        }
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="tolerance")
        result = ArtifactScorer().score("", task_dir)
        # 1/3 + 1/3 + 1/3 ≈ 1.0 within floating point tolerance
        assert result.error is None or "weight" not in result.error.lower()
        assert result.score == pytest.approx(1.0)

    def test_negative_weight_error(self, tmp_path: Path) -> None:
        gt = {
            "checks": [
                {"answer_type": "count", "answer": 1, "weight": -0.5},
                {"answer_type": "boolean", "answer": "true", "weight": 1.5},
            ],
        }
        answer = {"answers": []}
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="neg-weight")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == 0.0
        assert result.error is not None

    def test_non_numeric_weight_error(self, tmp_path: Path) -> None:
        gt = {
            "checks": [
                {"answer_type": "count", "answer": 1, "weight": "half"},
            ],
        }
        answer = {"answers": []}
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="non-numeric")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == 0.0
        assert result.error is not None

    def test_missing_weight_field_error(self, tmp_path: Path) -> None:
        gt = {
            "checks": [
                {"answer_type": "count", "answer": 1},
            ],
        }
        answer = {"answers": []}
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="no-weight")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == 0.0
        assert result.error is not None


# ---------------------------------------------------------------------------
# Check validation
# ---------------------------------------------------------------------------


class TestV2CheckValidation:
    """Individual check fields must be present and valid."""

    def test_empty_checks_array_error(self, tmp_path: Path) -> None:
        gt = {"checks": []}
        answer = {"answers": []}
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="empty-checks")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == 0.0
        assert result.error is not None

    def test_check_missing_answer_type_error(self, tmp_path: Path) -> None:
        gt = {
            "checks": [
                {"answer": 1, "weight": 1.0},
            ],
        }
        answer = {"answers": []}
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="no-type")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == 0.0
        assert result.error is not None

    def test_check_missing_answer_error(self, tmp_path: Path) -> None:
        gt = {
            "checks": [
                {"answer_type": "count", "weight": 1.0},
            ],
        }
        answer = {"answers": []}
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="no-answer")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == 0.0
        assert result.error is not None

    def test_unknown_answer_type_scores_zero_others_still_score(
        self, tmp_path: Path
    ) -> None:
        """Unknown type gets 0.0, but known checks still score normally."""
        gt = {
            "checks": [
                {"answer_type": "unknown_type", "answer": "x", "weight": 0.4},
                {"answer_type": "count", "answer": 5, "weight": 0.6},
            ],
        }
        answer = {
            "answers": [
                {"answer_type": "unknown_type", "answer": "x"},
                {"answer_type": "count", "answer": 5},
            ],
        }
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="unknown-type")
        result = ArtifactScorer().score("", task_dir)
        # unknown = 0.0, count = 1.0 → composite = 0.4*0 + 0.6*1 = 0.6
        assert result.score == pytest.approx(0.6)
        assert result.passed is True  # 0.6 >= 0.5


# ---------------------------------------------------------------------------
# Answer matching
# ---------------------------------------------------------------------------


class TestV2AnswerMatching:
    """Agent answers are matched to checks by answer_type."""

    def test_answers_array_matches_by_type(self, tmp_path: Path) -> None:
        """Order in answers array doesn't matter — match by answer_type."""
        gt = {
            "checks": [
                {"answer_type": "count", "answer": 3, "weight": 0.5},
                {"answer_type": "boolean", "answer": "true", "weight": 0.5},
            ],
        }
        # Reversed order in agent answers
        answer = {
            "answers": [
                {"answer_type": "boolean", "answer": "true"},
                {"answer_type": "count", "answer": 3},
            ],
        }
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="match-order")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == pytest.approx(1.0)

    def test_v1_answer_fallback_first_check_only(self, tmp_path: Path) -> None:
        """V1 answer.json (single 'answer') → first check uses it, others get 0."""
        gt = {
            "checks": [
                {"answer_type": "count", "answer": 5, "weight": 0.6},
                {"answer_type": "boolean", "answer": "true", "weight": 0.4},
            ],
        }
        # V1-style answer (no "answers" array, just "answer")
        answer = {"answer": 5, "answer_type": "count"}
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="v1-fallback")
        result = ArtifactScorer().score("", task_dir)
        # count check: matched via v1 fallback → 1.0
        # boolean check: no answer → 0.0
        # composite = 0.6 * 1.0 + 0.4 * 0.0 = 0.6
        assert result.score == pytest.approx(0.6)

    def test_missing_answer_for_check_scores_zero(self, tmp_path: Path) -> None:
        """Agent doesn't provide answer for a check type → that check = 0.0."""
        gt = {
            "checks": [
                {"answer_type": "count", "answer": 3, "weight": 0.5},
                {"answer_type": "boolean", "answer": "true", "weight": 0.5},
            ],
        }
        answer = {
            "answers": [
                {"answer_type": "count", "answer": 3},
                # No boolean answer provided
            ],
        }
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="missing-answer")
        result = ArtifactScorer().score("", task_dir)
        # count=1.0, boolean=0.0 → 0.5*1 + 0.5*0 = 0.5
        assert result.score == pytest.approx(0.5)
        assert result.passed is True  # exactly at PASS_THRESHOLD

    def test_duplicate_answer_type_uses_first(self, tmp_path: Path) -> None:
        """Multiple answers with same type → use the first match."""
        gt = {
            "checks": [
                {"answer_type": "count", "answer": 3, "weight": 1.0},
            ],
        }
        answer = {
            "answers": [
                {"answer_type": "count", "answer": 3},
                {"answer_type": "count", "answer": 99},
            ],
        }
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="dup-type")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    """V1 and legacy formats must still work identically."""

    def test_v1_single_answer_format(self, tmp_path: Path) -> None:
        gt = {"answer_type": "count", "answer": 5}
        answer = {"answer": 5}
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="v1-compat")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_v1_single_answer_format_fail(self, tmp_path: Path) -> None:
        gt = {"answer_type": "count", "answer": 5}
        answer = {"answer": 99}
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="v1-fail")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == pytest.approx(0.0)
        assert result.passed is False

    def test_legacy_expected_format(self, tmp_path: Path) -> None:
        gt = {"expected": ["a.py", "b.py"]}
        answer = {"answer": ["a.py", "b.py"]}
        task_dir = _make_artifact_task(tmp_path, gt, answer, name="legacy-compat")
        result = ArtifactScorer().score("", task_dir)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True


# ---------------------------------------------------------------------------
# validate_ground_truth
# ---------------------------------------------------------------------------


class TestValidateGroundTruth:
    """Test the standalone validation function."""

    def test_valid_v1(self) -> None:
        from codeprobe.core.scoring import validate_ground_truth

        assert validate_ground_truth({"answer_type": "count", "answer": 5}) is None

    def test_valid_v2(self) -> None:
        from codeprobe.core.scoring import validate_ground_truth

        gt = {
            "checks": [
                {"answer_type": "count", "answer": 5, "weight": 1.0},
            ],
        }
        assert validate_ground_truth(gt) is None

    def test_valid_legacy(self) -> None:
        from codeprobe.core.scoring import validate_ground_truth

        assert validate_ground_truth({"expected": ["a.py"]}) is None

    def test_v1_missing_answer_type(self) -> None:
        from codeprobe.core.scoring import validate_ground_truth

        err = validate_ground_truth({"answer": 5})
        assert err is not None
        assert "answer_type" in err or "checks" in err or "expected" in err

    def test_v2_empty_checks(self) -> None:
        from codeprobe.core.scoring import validate_ground_truth

        err = validate_ground_truth({"checks": []})
        assert err is not None

    def test_v2_check_missing_weight(self) -> None:
        from codeprobe.core.scoring import validate_ground_truth

        gt = {"checks": [{"answer_type": "count", "answer": 1}]}
        err = validate_ground_truth(gt)
        assert err is not None

    def test_v2_weights_dont_sum_to_one(self) -> None:
        from codeprobe.core.scoring import validate_ground_truth

        gt = {
            "checks": [
                {"answer_type": "count", "answer": 1, "weight": 0.3},
                {"answer_type": "boolean", "answer": "true", "weight": 0.3},
            ],
        }
        err = validate_ground_truth(gt)
        assert err is not None

    def test_v2_valid_weights_sum(self) -> None:
        from codeprobe.core.scoring import validate_ground_truth

        gt = {
            "checks": [
                {"answer_type": "count", "answer": 1, "weight": 0.7},
                {"answer_type": "boolean", "answer": "true", "weight": 0.3},
            ],
        }
        assert validate_ground_truth(gt) is None

    def test_legacy_expected_not_list(self) -> None:
        from codeprobe.core.scoring import validate_ground_truth

        err = validate_ground_truth({"expected": "not-a-list"})
        assert err is not None

    def test_nan_weight_rejected(self) -> None:
        from codeprobe.core.scoring import validate_ground_truth

        gt = {
            "checks": [
                {"answer_type": "count", "answer": 5, "weight": float("nan")},
            ],
        }
        err = validate_ground_truth(gt)
        assert err is not None
        assert "finite" in err.lower()

    def test_inf_weight_rejected(self) -> None:
        from codeprobe.core.scoring import validate_ground_truth

        gt = {
            "checks": [
                {"answer_type": "count", "answer": 5, "weight": float("inf")},
            ],
        }
        err = validate_ground_truth(gt)
        assert err is not None
        assert "finite" in err.lower()
