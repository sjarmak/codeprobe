"""Unit tests for codeprobe.analysis.dual.dual_composite."""

from __future__ import annotations

import pytest

from codeprobe.analysis.dual import dual_composite, has_dual_scoring
from codeprobe.models.experiment import CompletedTask


def _dual_task(
    *,
    direct: float,
    artifact: float,
    passed_direct: bool | None = None,
    passed_artifact: bool | None = None,
    policy: str = "min",
    automated_score: float = 0.0,
) -> CompletedTask:
    """Build a CompletedTask carrying dual scoring_details."""
    details: dict = {
        "score_direct": direct,
        "score_artifact": artifact,
        "scoring_policy": policy,
    }
    if passed_direct is not None:
        details["passed_direct"] = passed_direct
    if passed_artifact is not None:
        details["passed_artifact"] = passed_artifact
    return CompletedTask(
        task_id="t",
        automated_score=automated_score,
        scoring_details=details,
    )


# ---------------------------------------------------------------------------
# has_dual_scoring
# ---------------------------------------------------------------------------


def test_has_dual_scoring_true_for_score_direct() -> None:
    task = CompletedTask(
        task_id="t", automated_score=0.0, scoring_details={"score_direct": 0.5}
    )
    assert has_dual_scoring(task) is True


def test_has_dual_scoring_false_for_empty() -> None:
    task = CompletedTask(task_id="t", automated_score=1.0, scoring_details={})
    assert has_dual_scoring(task) is False


def test_has_dual_scoring_false_for_unrelated_details() -> None:
    task = CompletedTask(
        task_id="t", automated_score=1.0, scoring_details={"note": "ok"}
    )
    assert has_dual_scoring(task) is False


# ---------------------------------------------------------------------------
# strategies
# ---------------------------------------------------------------------------


def test_min_strategy() -> None:
    task = _dual_task(direct=0.8, artifact=0.6)
    assert dual_composite(task, strategy="min") == pytest.approx(0.6)


def test_min_strategy_is_default() -> None:
    task = _dual_task(direct=0.9, artifact=0.3)
    assert dual_composite(task) == pytest.approx(0.3)


def test_mean_strategy() -> None:
    task = _dual_task(direct=0.8, artifact=0.6)
    assert dual_composite(task, strategy="mean") == pytest.approx(0.7)


def test_mean_strategy_zeros() -> None:
    task = _dual_task(direct=0.0, artifact=0.0)
    assert dual_composite(task, strategy="mean") == 0.0


def test_gate_strategy_both_passed_flags() -> None:
    task = _dual_task(
        direct=0.4,
        artifact=0.4,
        passed_direct=True,
        passed_artifact=True,
    )
    assert dual_composite(task, strategy="gate") == 1.0


def test_gate_strategy_one_leg_failing_flag() -> None:
    task = _dual_task(
        direct=0.9,
        artifact=0.9,
        passed_direct=True,
        passed_artifact=False,
    )
    assert dual_composite(task, strategy="gate") == 0.0


def test_gate_strategy_uses_score_threshold_fallback() -> None:
    # No explicit passed_* flags — fall back to score >= PASS_THRESHOLD (0.5).
    task = _dual_task(direct=0.75, artifact=0.6)
    assert dual_composite(task, strategy="gate") == 1.0


def test_gate_strategy_below_threshold_fails() -> None:
    task = _dual_task(direct=0.75, artifact=0.3)
    assert dual_composite(task, strategy="gate") == 0.0


# ---------------------------------------------------------------------------
# fallback behaviour
# ---------------------------------------------------------------------------


def test_fallback_for_task_without_scoring_details() -> None:
    task = CompletedTask(task_id="t", automated_score=0.42, scoring_details={})
    assert dual_composite(task, strategy="min") == pytest.approx(0.42)


def test_fallback_for_unrelated_scoring_details() -> None:
    task = CompletedTask(
        task_id="t",
        automated_score=0.75,
        scoring_details={"comment": "hello"},
    )
    assert dual_composite(task, strategy="mean") == pytest.approx(0.75)


# ---------------------------------------------------------------------------
# error cases
# ---------------------------------------------------------------------------


def test_unknown_strategy_raises() -> None:
    task = _dual_task(direct=0.5, artifact=0.5)
    with pytest.raises(ValueError, match="unknown dual_composite strategy"):
        dual_composite(task, strategy="bogus")
