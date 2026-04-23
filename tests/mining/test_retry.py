"""Retry wrapper + threshold tests (r15-incremental-mining, INV1)."""

from __future__ import annotations

import pytest

from codeprobe.mining.retry import RetryLimitExceeded, RetryTracker, retry_call


def test_successful_call_counts_one_attempt() -> None:
    tracker = RetryTracker(min_attempts=1)
    result = retry_call(lambda: 42, tracker=tracker, retries=3)
    assert result == 42
    assert tracker.attempts == 1
    assert tracker.exhausted == 0


def test_transient_failure_recovers_within_retries() -> None:
    tracker = RetryTracker(min_attempts=1)
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return "ok"

    result = retry_call(flaky, tracker=tracker, retries=3, backoff=0.0)
    assert result == "ok"
    assert tracker.attempts == 3
    assert tracker.exhausted == 0


def test_exhausted_retries_increment_counter_and_reraise() -> None:
    tracker = RetryTracker(min_attempts=10_000, ratio=0.001)

    def always_fail() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        retry_call(always_fail, tracker=tracker, retries=2, backoff=0.0)

    assert tracker.attempts == 3
    assert tracker.exhausted == 1


def test_below_threshold_does_not_abort() -> None:
    """1 exhausted / 1000 attempts sits at the 0.1% boundary → no abort."""
    tracker = RetryTracker(min_attempts=100, ratio=0.001)
    tracker.attempts = 1000
    tracker.exhausted = 1
    # Strict > means equal ratio passes.
    tracker.check_threshold()


def test_above_threshold_aborts_with_retry_limit_exceeded() -> None:
    tracker = RetryTracker(min_attempts=100, ratio=0.001)
    tracker.attempts = 1000
    tracker.exhausted = 2
    with pytest.raises(RetryLimitExceeded):
        tracker.check_threshold()


def test_min_attempts_prevents_early_abort() -> None:
    """Small runs should not abort on a single exhausted retry."""
    tracker = RetryTracker(min_attempts=100, ratio=0.001)
    tracker.attempts = 5
    tracker.exhausted = 5  # 100% fail ratio
    # Below min_attempts — no abort even though the ratio is huge.
    tracker.check_threshold()


def test_mine_aborts_when_budget_crossed_via_retry_call() -> None:
    """A series of exhausted retries that cross the ratio aborts the mine.

    Simulates the INV1 contract: once >0.1% of attempts exhaust their
    retries, the NEXT exhausted retry_call propagates RetryLimitExceeded
    instead of the transient exception.
    """
    tracker = RetryTracker(min_attempts=100, ratio=0.001)
    # Pre-load the tracker just below the threshold.
    tracker.attempts = 1000
    tracker.exhausted = 1

    # One more exhausted call pushes the ratio above 0.001 (2/1001).
    def always_fail() -> None:
        raise RuntimeError("sustained failure")

    with pytest.raises(RetryLimitExceeded):
        retry_call(always_fail, tracker=tracker, retries=0, backoff=0.0)


def test_non_caught_exceptions_propagate_without_retry() -> None:
    """Exceptions outside *exceptions* tuple skip the retry loop entirely."""
    tracker = RetryTracker(min_attempts=1)

    def wrong() -> None:
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        retry_call(
            wrong, tracker=tracker, retries=3, backoff=0.0, exceptions=(ValueError,)
        )
    assert tracker.attempts == 1
    assert tracker.exhausted == 0


def test_retry_history_is_bounded() -> None:
    """``history`` should not grow without bound on a long-running mine."""
    tracker = RetryTracker()
    for i in range(500):
        tracker.record_exhaustion(detail=f"fail-{i}")
    assert len(tracker.history) <= 100
