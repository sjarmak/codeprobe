"""Tests for _safe_leg_score — exception handling in DualScorer legs."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from codeprobe.core.scoring import ScoreResult, _safe_leg_score

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _PassScorer:
    """Scorer stub that always succeeds."""

    def score(self, agent_output: str, task_dir: Path) -> ScoreResult:
        return ScoreResult(score=1.0, passed=True)


class _RaisingScorer:
    """Scorer stub that raises a configurable exception."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def score(self, agent_output: str, task_dir: Path) -> ScoreResult:
        raise self._exc


# ---------------------------------------------------------------------------
# Tests: error string includes exception type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("bad value"),
        PermissionError("access denied"),
        RecursionError("maximum recursion depth exceeded"),
        RuntimeError("something broke"),
    ],
    ids=["ValueError", "PermissionError", "RecursionError", "RuntimeError"],
)
def test_error_includes_exception_type(tmp_path: Path, exc: Exception) -> None:
    """ScoreResult.error must contain the exception class name."""
    scorer = _RaisingScorer(exc)
    result = _safe_leg_score(scorer, "", tmp_path)

    assert result.score == 0.0
    assert result.passed is False
    assert type(exc).__name__ in result.error
    assert str(exc) in result.error


# ---------------------------------------------------------------------------
# Tests: logger.exception called at ERROR level
# ---------------------------------------------------------------------------


def test_logs_exception_at_error_level(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """logger.exception must fire at ERROR level with scorer name and task_dir."""
    exc = ValueError("kaboom")
    scorer = _RaisingScorer(exc)

    with caplog.at_level(logging.ERROR, logger="codeprobe.core.scoring"):
        _safe_leg_score(scorer, "", tmp_path)

    assert len(caplog.records) >= 1
    record = caplog.records[0]
    assert record.levelno == logging.ERROR
    assert "_RaisingScorer" in record.message
    assert str(tmp_path) in record.message
    # logger.exception attaches exc_info
    assert record.exc_info is not None


# ---------------------------------------------------------------------------
# Tests: happy path still works
# ---------------------------------------------------------------------------


def test_successful_scorer_returns_normally(tmp_path: Path) -> None:
    """A scorer that does not raise should return its result unchanged."""
    scorer = _PassScorer()
    result = _safe_leg_score(scorer, "", tmp_path)

    assert result.score == 1.0
    assert result.passed is True
    assert result.error is None


# ---------------------------------------------------------------------------
# Tests: both legs run when one fails (DualScorer contract)
# ---------------------------------------------------------------------------


def test_both_legs_run_when_one_raises(tmp_path: Path) -> None:
    """Even if leg-A raises, leg-B must still execute and return its result."""
    scorer_a = _RaisingScorer(RuntimeError("leg-A failed"))
    scorer_b = _PassScorer()

    result_a = _safe_leg_score(scorer_a, "", tmp_path)
    result_b = _safe_leg_score(scorer_b, "", tmp_path)

    # leg-A captured the error
    assert result_a.score == 0.0
    assert result_a.passed is False
    assert "RuntimeError" in result_a.error

    # leg-B ran successfully
    assert result_b.score == 1.0
    assert result_b.passed is True
