"""Tests for oracle scorer registry — answer_type dispatch via registry pattern."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.core.scoring import (
    PASS_THRESHOLD,
    ArtifactScorer,
    ScoreResult,
    _ORACLE_TYPE_SCORERS,
    score_count,
    score_exact_match,
    score_file_list,
)


class TestOracleTypeScorerFunctions:
    """Test that module-level oracle scoring functions work correctly."""

    def test_score_file_list_perfect(self):
        result = score_file_list(["a.py", "b.py"], ["a.py", "b.py"])
        assert result.score == 1.0
        assert result.passed is True

    def test_score_file_list_partial(self):
        result = score_file_list(["a.py", "b.py", "c.py"], ["a.py", "b.py"])
        assert 0.0 < result.score < 1.0

    def test_score_file_list_empty_actual(self):
        result = score_file_list(["a.py"], [])
        assert result.score == 0.0
        assert result.passed is False

    def test_score_count_match(self):
        result = score_count(42, 42)
        assert result.score == 1.0
        assert result.passed is True

    def test_score_count_mismatch(self):
        result = score_count(42, 99)
        assert result.score == 0.0
        assert result.passed is False

    def test_score_count_string_coercion(self):
        result = score_count("5", "5")
        assert result.passed is True

    def test_score_count_invalid(self):
        result = score_count("abc", "5")
        assert result.passed is False
        assert result.error is not None

    def test_score_exact_match_boolean_true(self):
        result = score_exact_match("true", "True")
        assert result.passed is True

    def test_score_exact_match_boolean_false(self):
        result = score_exact_match("true", "false")
        assert result.passed is False

    def test_score_exact_match_text(self):
        result = score_exact_match("hello", "  Hello  ")
        assert result.passed is True


class TestOracleRegistryMapping:
    """Test that _ORACLE_TYPE_SCORERS dict maps correctly."""

    def test_file_list_registered(self):
        assert "file_list" in _ORACLE_TYPE_SCORERS
        assert _ORACLE_TYPE_SCORERS["file_list"] is score_file_list

    def test_count_registered(self):
        assert "count" in _ORACLE_TYPE_SCORERS
        assert _ORACLE_TYPE_SCORERS["count"] is score_count

    def test_boolean_registered(self):
        assert "boolean" in _ORACLE_TYPE_SCORERS
        assert _ORACLE_TYPE_SCORERS["boolean"] is score_exact_match

    def test_text_registered(self):
        assert "text" in _ORACLE_TYPE_SCORERS
        assert _ORACLE_TYPE_SCORERS["text"] is score_exact_match


class TestOracleRegistryExtensibility:
    """Test entry_point extensibility via registry.py."""

    def test_resolve_builtin_oracle_scorer(self):
        from codeprobe.core.registry import resolve_oracle_scorer

        scorer = resolve_oracle_scorer("file_list")
        # Builtins resolve to the callable itself
        assert callable(scorer)

    def test_resolve_unknown_oracle_scorer_raises(self):
        from codeprobe.core.registry import resolve_oracle_scorer

        with pytest.raises(KeyError, match="Unknown oracle scorer"):
            resolve_oracle_scorer("nonexistent_type")

    def test_available_oracle_scorers_includes_builtins(self):
        from codeprobe.core.registry import available_oracle_scorers

        names = available_oracle_scorers()
        assert "file_list" in names
        assert "count" in names
        assert "boolean" in names
        assert "text" in names

    def test_entry_point_extension(self):
        """Mock an entry_point to verify extensibility."""
        from unittest.mock import MagicMock

        mock_ep = MagicMock()
        mock_ep.name = "custom_type"
        mock_ep.load.return_value = lambda expected, actual: ScoreResult(
            score=1.0, passed=True
        )

        with patch("importlib.metadata.entry_points", return_value=[mock_ep]):
            from codeprobe.core.registry import resolve_oracle_scorer

            scorer = resolve_oracle_scorer("custom_type")
            assert callable(scorer)


class TestArtifactScorerNewFormatRegression:
    """Regression: _score_new_format produces identical results via registry."""

    @pytest.fixture
    def scorer(self):
        return ArtifactScorer()

    @pytest.fixture
    def task_dir(self, tmp_path: Path):
        task = tmp_path / "task"
        task.mkdir()
        return task

    def _write_files(self, task_dir: Path, gt: dict, answer: dict):
        (task_dir / "ground_truth.json").write_text(json.dumps(gt))
        (task_dir / "answer.json").write_text(json.dumps(answer))

    def test_file_list_scoring(self, scorer, task_dir):
        self._write_files(
            task_dir,
            {"answer_type": "file_list", "answer": ["a.py", "b.py"]},
            {"answer": ["a.py", "b.py"]},
        )
        result = scorer.score("", task_dir)
        assert result.score == 1.0
        assert result.passed is True

    def test_count_scoring(self, scorer, task_dir):
        self._write_files(
            task_dir,
            {"answer_type": "count", "answer": 5},
            {"answer": 5},
        )
        result = scorer.score("", task_dir)
        assert result.score == 1.0
        assert result.passed is True

    def test_boolean_scoring(self, scorer, task_dir):
        self._write_files(
            task_dir,
            {"answer_type": "boolean", "answer": "true"},
            {"answer": "True"},
        )
        result = scorer.score("", task_dir)
        assert result.passed is True

    def test_text_scoring(self, scorer, task_dir):
        self._write_files(
            task_dir,
            {"answer_type": "text", "answer": "hello"},
            {"answer": "  Hello  "},
        )
        result = scorer.score("", task_dir)
        assert result.passed is True

    def test_unknown_type_error(self, scorer, task_dir):
        self._write_files(
            task_dir,
            {"answer_type": "unknown_xyz", "answer": "foo"},
            {"answer": "bar"},
        )
        result = scorer.score("", task_dir)
        assert result.passed is False
        assert "Unknown answer_type" in result.error

    def test_missing_expected(self, scorer, task_dir):
        self._write_files(
            task_dir,
            {"answer_type": "count"},
            {"answer": 5},
        )
        result = scorer.score("", task_dir)
        assert result.passed is False
        assert "missing 'answer' field" in result.error

    def test_missing_actual(self, scorer, task_dir):
        self._write_files(
            task_dir,
            {"answer_type": "count", "answer": 5},
            {"something_else": 5},
        )
        result = scorer.score("", task_dir)
        assert result.passed is False
        assert "missing 'answer' field" in result.error
