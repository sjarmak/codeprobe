"""Tests for ArtifactScorer — all 4 answer_type variants, legacy format, and confidence warning."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from codeprobe.core.scoring import ArtifactScorer


@pytest.fixture()
def scorer() -> ArtifactScorer:
    return ArtifactScorer()


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# file_list answer_type — F1 scoring
# ---------------------------------------------------------------------------


class TestFileList:
    def test_perfect_match(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {
                "answer_type": "file_list",
                "answer": ["src/a.py", "src/b.py"],
                "confidence": 0.9,
            },
        )
        _write_json(
            tmp_path / "answer.json",
            {"answer": ["src/a.py", "src/b.py"]},
        )
        result = scorer.score("", tmp_path)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_partial_match(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {
                "answer_type": "file_list",
                "answer": ["a.py", "b.py", "c.py"],
                "confidence": 0.9,
            },
        )
        _write_json(
            tmp_path / "answer.json",
            {"answer": ["a.py", "b.py"]},
        )
        result = scorer.score("", tmp_path)
        # codeprobe-voxa (revised): default family is oracle_overlap_f1,
        # so F1 ≈ 0.8 is the headline reward. recall = 2/3 stays in
        # sub_scores / ir_metrics for diagnostics.
        assert result.score == pytest.approx(0.8, abs=0.01)
        assert result.reward_score == pytest.approx(0.8, abs=0.01)
        assert result.scorer_family == "oracle_overlap_f1"
        assert result.ir_metrics["recall"] == pytest.approx(2 / 3, abs=0.01)
        assert result.ir_metrics["precision"] == pytest.approx(1.0)
        assert result.ir_metrics["f1"] == pytest.approx(0.8, abs=0.01)
        assert result.passed is True

    def test_no_match(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "file_list", "answer": ["a.py"], "confidence": 0.9},
        )
        _write_json(
            tmp_path / "answer.json",
            {"answer": ["z.py"]},
        )
        result = scorer.score("", tmp_path)
        assert result.score == pytest.approx(0.0)
        assert result.passed is False

    def test_path_normalization(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {
                "answer_type": "file_list",
                "answer": ["./src/a.py", "/workspace/src/b.py"],
                "confidence": 0.9,
            },
        )
        _write_json(
            tmp_path / "answer.json",
            {"answer": ["src/a.py", "src/b.py"]},
        )
        result = scorer.score("", tmp_path)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True


# ---------------------------------------------------------------------------
# count answer_type
# ---------------------------------------------------------------------------


class TestCount:
    def test_exact_match(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "count", "answer": 42, "confidence": 0.9},
        )
        _write_json(tmp_path / "answer.json", {"answer": 42})
        result = scorer.score("", tmp_path)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_string_int_match(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "count", "answer": 7, "confidence": 0.9},
        )
        _write_json(tmp_path / "answer.json", {"answer": "7"})
        result = scorer.score("", tmp_path)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_mismatch(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "count", "answer": 10, "confidence": 0.9},
        )
        _write_json(tmp_path / "answer.json", {"answer": 11})
        result = scorer.score("", tmp_path)
        assert result.score == pytest.approx(0.0)
        assert result.passed is False


# ---------------------------------------------------------------------------
# boolean answer_type
# ---------------------------------------------------------------------------


class TestBoolean:
    def test_true_match(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "boolean", "answer": "true", "confidence": 0.9},
        )
        _write_json(tmp_path / "answer.json", {"answer": "True"})
        result = scorer.score("", tmp_path)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_false_match(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "boolean", "answer": "false", "confidence": 0.9},
        )
        _write_json(tmp_path / "answer.json", {"answer": "FALSE"})
        result = scorer.score("", tmp_path)
        assert result.score == pytest.approx(1.0)

    def test_mismatch(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "boolean", "answer": "true", "confidence": 0.9},
        )
        _write_json(tmp_path / "answer.json", {"answer": "false"})
        result = scorer.score("", tmp_path)
        assert result.score == pytest.approx(0.0)
        assert result.passed is False


# ---------------------------------------------------------------------------
# text answer_type
# ---------------------------------------------------------------------------


class TestText:
    def test_exact_match(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "text", "answer": "Hello World", "confidence": 0.9},
        )
        _write_json(tmp_path / "answer.json", {"answer": "hello world"})
        result = scorer.score("", tmp_path)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_whitespace_tolerance(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "text", "answer": "  answer  ", "confidence": 0.9},
        )
        _write_json(tmp_path / "answer.json", {"answer": "answer"})
        result = scorer.score("", tmp_path)
        assert result.score == pytest.approx(1.0)

    def test_mismatch(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "text", "answer": "foo", "confidence": 0.9},
        )
        _write_json(tmp_path / "answer.json", {"answer": "bar"})
        result = scorer.score("", tmp_path)
        assert result.score == pytest.approx(0.0)
        assert result.passed is False


# ---------------------------------------------------------------------------
# Legacy ground_truth.json format
# ---------------------------------------------------------------------------


class TestLegacyFormat:
    def test_legacy_expected_key(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {
                "schema_version": 1,
                "oracle_type": "file_list",
                "expected": ["a.py", "b.py"],
            },
        )
        _write_json(tmp_path / "answer.json", {"answer": ["a.py", "b.py"]})
        result = scorer.score("", tmp_path)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_legacy_partial(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {
                "schema_version": 1,
                "oracle_type": "file_list",
                "expected": ["a.py", "b.py", "c.py"],
            },
        )
        _write_json(tmp_path / "answer.json", {"answer": ["a.py"]})
        result = scorer.score("", tmp_path)
        # codeprobe-voxa (revised): legacy IR format honors the same
        # default family. Reward = F1 = 0.5 (recall=1/3, precision=1.0).
        # Recall stays in sub_scores / ir_metrics.
        assert result.score == pytest.approx(0.5, abs=0.01)
        assert result.reward_score == pytest.approx(0.5, abs=0.01)
        assert result.scorer_family == "oracle_overlap_f1"
        assert result.ir_metrics["recall"] == pytest.approx(1 / 3, abs=0.01)
        assert result.ir_metrics["precision"] == pytest.approx(1.0)
        assert result.ir_metrics["f1"] == pytest.approx(0.5, abs=0.01)
        # 0.5 is exactly at PASS_THRESHOLD — passed is True under >= semantics
        assert result.passed is True


# ---------------------------------------------------------------------------
# Confidence warning
# ---------------------------------------------------------------------------


class TestConfidenceWarning:
    def test_low_confidence_warns(
        self, tmp_path: Path, scorer: ArtifactScorer, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "text", "answer": "x", "confidence": 0.3},
        )
        _write_json(tmp_path / "answer.json", {"answer": "x"})
        with caplog.at_level(logging.WARNING):
            result = scorer.score("", tmp_path)
        assert result.passed is True
        assert "Low confidence" in caplog.text

    def test_high_confidence_no_warning(
        self, tmp_path: Path, scorer: ArtifactScorer, caplog: pytest.LogCaptureFixture
    ) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "text", "answer": "x", "confidence": 0.9},
        )
        _write_json(tmp_path / "answer.json", {"answer": "x"})
        with caplog.at_level(logging.WARNING):
            scorer.score("", tmp_path)
        assert "Low confidence" not in caplog.text

    def test_custom_threshold_silences_warning_below_default(
        self, tmp_path: Path, scorer: ArtifactScorer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """codeprobe-kdng: low_confidence_threshold is config-plumbed, not
        hardcoded — a caller-supplied threshold below the confidence value
        silences the warning that the historical 0.5 default would emit."""
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "text", "answer": "x", "confidence": 0.4},
        )
        _write_json(tmp_path / "answer.json", {"answer": "x"})
        with caplog.at_level(logging.WARNING):
            scorer.score("", tmp_path, low_confidence_threshold=0.3)
        assert "Low confidence" not in caplog.text

    def test_custom_threshold_raises_warning_above_default(
        self, tmp_path: Path, scorer: ArtifactScorer, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A confidence that would pass the 0.5 default still warns when the
        caller supplies a stricter (higher) threshold."""
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "text", "answer": "x", "confidence": 0.6},
        )
        _write_json(tmp_path / "answer.json", {"answer": "x"})
        with caplog.at_level(logging.WARNING):
            scorer.score("", tmp_path, low_confidence_threshold=0.7)
        assert "Low confidence" in caplog.text


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_missing_ground_truth(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(tmp_path / "answer.json", {"answer": "x"})
        result = scorer.score("", tmp_path)
        assert result.passed is False
        assert result.error is not None
        assert "ground_truth" in result.error

    def test_missing_answer(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "text", "answer": "x", "confidence": 0.9},
        )
        result = scorer.score("", tmp_path)
        assert result.passed is False
        assert result.error is not None
        assert "answer.json" in result.error

    def test_answer_in_tests_subdir(
        self, tmp_path: Path, scorer: ArtifactScorer
    ) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "text", "answer": "hello", "confidence": 0.9},
        )
        _write_json(tmp_path / "tests" / "answer.json", {"answer": "hello"})
        result = scorer.score("", tmp_path)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_unknown_answer_type(self, tmp_path: Path, scorer: ArtifactScorer) -> None:
        _write_json(
            tmp_path / "ground_truth.json",
            {"answer_type": "unknown_type", "answer": "x", "confidence": 0.9},
        )
        # The agent's answer matches the oracle exactly; only the declared
        # answer_type is unscoreable. That is harness breakage, so it must
        # be a verifier_error rather than a 0.0 charged to the agent
        # (codeprobe-sh8c). This assertion previously encoded the bug.
        _write_json(tmp_path / "answer.json", {"answer": "x"})
        result = scorer.score("", tmp_path)
        assert result.passed is False
        assert result.verdict == "verifier_error"
        assert "unknown answer_type" in (result.error or "")
