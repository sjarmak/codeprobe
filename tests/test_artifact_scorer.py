"""Tests for ArtifactScorer — all 4 answer_type variants, legacy format, and confidence warning."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from codeprobe.core.scoring import ArtifactScorer, ScoreResult


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
        # precision = 2/2 = 1.0, recall = 2/3 ≈ 0.667, F1 ≈ 0.8
        assert result.score == pytest.approx(0.8, abs=0.01)
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
        # precision = 1/1 = 1.0, recall = 1/3 ≈ 0.333, F1 = 0.5
        assert result.score == pytest.approx(0.5, abs=0.01)
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
        _write_json(tmp_path / "answer.json", {"answer": "x"})
        result = scorer.score("", tmp_path)
        assert result.passed is False
        assert "Unknown answer_type" in (result.error or "")
