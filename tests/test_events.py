"""Tests for core.events — typed events, dispatcher, and budget checker."""

from __future__ import annotations

import threading
import time
from dataclasses import FrozenInstanceError

import pytest

from codeprobe.core.events import (
    BudgetChecker,
    BudgetWarning,
    EventDispatcher,
    RunEvent,
    RunFinished,
    RunStarted,
    TaskScored,
    TaskStarted,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingListener:
    """Collects every event it receives."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def on_event(self, event: RunEvent) -> None:
        self.events.append(event)


def _make_task_scored(
    *,
    task_id: str = "t1",
    cost_usd: float | None = 1.0,
    cost_model: str = "per_token",
    score: float = 1.0,
) -> TaskScored:
    return TaskScored(
        task_id=task_id,
        config_label="baseline",
        automated_score=score,
        duration_seconds=5.0,
        cost_usd=cost_usd,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=10,
        cost_model=cost_model,
        cost_source="api_response",
        error=None,
        timestamp=time.time(),
    )


# ---------------------------------------------------------------------------
# Event creation (frozen)
# ---------------------------------------------------------------------------


class TestEventCreation:
    def test_run_started(self) -> None:
        ev = RunStarted(total_tasks=5, config_label="baseline", timestamp=1.0)
        assert ev.total_tasks == 5
        assert ev.config_label == "baseline"
        assert ev.timestamp == 1.0

    def test_task_started(self) -> None:
        ev = TaskStarted(task_id="abc", config_label="mcp", timestamp=2.0)
        assert ev.task_id == "abc"

    def test_task_scored(self) -> None:
        ev = _make_task_scored(task_id="xyz", cost_usd=0.5, score=0.8)
        assert ev.task_id == "xyz"
        assert ev.cost_usd == 0.5
        assert ev.automated_score == 0.8

    def test_budget_warning(self) -> None:
        ev = BudgetWarning(
            cumulative_cost=8.0, budget=10.0, threshold_pct=0.8, timestamp=3.0
        )
        assert ev.cumulative_cost == 8.0

    def test_run_finished(self) -> None:
        ev = RunFinished(
            total_tasks=10,
            completed_count=9,
            mean_score=0.75,
            total_cost=4.5,
            total_duration=120.0,
            config_label="test",
            timestamp=4.0,
        )
        assert ev.completed_count == 9

    @pytest.mark.parametrize(
        "event",
        [
            RunStarted(total_tasks=1, config_label="x", timestamp=0.0),
            TaskStarted(task_id="a", config_label="x", timestamp=0.0),
            BudgetWarning(
                cumulative_cost=1.0, budget=2.0, threshold_pct=0.5, timestamp=0.0
            ),
            RunFinished(
                total_tasks=1,
                completed_count=1,
                mean_score=1.0,
                total_cost=0.0,
                total_duration=0.0,
                config_label="x",
                timestamp=0.0,
            ),
        ],
    )
    def test_events_are_frozen(self, event: RunEvent) -> None:
        with pytest.raises(FrozenInstanceError):
            event.timestamp = 999.0  # type: ignore[misc]

    def test_task_scored_is_frozen(self) -> None:
        ev = _make_task_scored()
        with pytest.raises(FrozenInstanceError):
            ev.task_id = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# EventDispatcher
# ---------------------------------------------------------------------------


class TestEventDispatcher:
    def test_delivers_to_multiple_listeners(self) -> None:
        dispatcher = EventDispatcher()
        l1 = RecordingListener()
        l2 = RecordingListener()
        dispatcher.register(l1)
        dispatcher.register(l2)

        ev = RunStarted(total_tasks=3, config_label="test", timestamp=time.time())
        dispatcher.emit(ev)
        dispatcher.shutdown()

        assert l1.events == [ev]
        assert l2.events == [ev]

    def test_emit_is_nonblocking(self) -> None:
        dispatcher = EventDispatcher()
        # Register a slow listener
        slow = RecordingListener()
        original_on_event = slow.on_event

        def slow_on_event(event: RunEvent) -> None:
            time.sleep(0.1)
            original_on_event(event)

        slow.on_event = slow_on_event  # type: ignore[assignment]
        dispatcher.register(slow)

        start = time.monotonic()
        for _ in range(5):
            dispatcher.emit(
                RunStarted(total_tasks=1, config_label="x", timestamp=time.time())
            )
        elapsed = time.monotonic() - start

        # All 5 emits should complete in well under 100ms
        assert elapsed < 0.05, f"emit() blocked for {elapsed:.3f}s"
        dispatcher.shutdown()
        assert len(slow.events) == 5

    def test_shutdown_drains_all_events(self) -> None:
        dispatcher = EventDispatcher()
        listener = RecordingListener()
        dispatcher.register(listener)

        for i in range(20):
            dispatcher.emit(
                TaskStarted(
                    task_id=f"task-{i}", config_label="drain", timestamp=time.time()
                )
            )
        dispatcher.shutdown()

        assert len(listener.events) == 20

    def test_thread_safety_concurrent_emits(self) -> None:
        dispatcher = EventDispatcher()
        listener = RecordingListener()
        dispatcher.register(listener)

        barrier = threading.Barrier(10)

        def emitter(idx: int) -> None:
            barrier.wait()
            dispatcher.emit(
                TaskStarted(
                    task_id=f"t-{idx}", config_label="par", timestamp=time.time()
                )
            )

        threads = [threading.Thread(target=emitter, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        dispatcher.shutdown()
        assert len(listener.events) == 10

    def test_listener_exception_does_not_crash_dispatcher(self) -> None:
        dispatcher = EventDispatcher()

        class BadListener:
            def on_event(self, event: RunEvent) -> None:
                raise RuntimeError("boom")

        good = RecordingListener()
        dispatcher.register(BadListener())
        dispatcher.register(good)

        ev = RunStarted(total_tasks=1, config_label="x", timestamp=time.time())
        dispatcher.emit(ev)
        dispatcher.shutdown()

        # Good listener still got the event despite the bad one raising
        assert good.events == [ev]


# ---------------------------------------------------------------------------
# BudgetChecker
# ---------------------------------------------------------------------------


class TestBudgetChecker:
    def test_accumulates_billable_cost(self) -> None:
        checker = BudgetChecker(budget=10.0)
        dispatcher = EventDispatcher()
        checker.set_dispatcher(dispatcher)
        dispatcher.register(checker)

        dispatcher.emit(_make_task_scored(cost_usd=3.0))
        dispatcher.emit(_make_task_scored(cost_usd=2.0))
        dispatcher.shutdown()

        assert checker.cumulative_cost == pytest.approx(5.0)

    def test_ignores_non_billable_cost_models(self) -> None:
        checker = BudgetChecker(budget=10.0)
        dispatcher = EventDispatcher()
        checker.set_dispatcher(dispatcher)
        dispatcher.register(checker)

        dispatcher.emit(_make_task_scored(cost_usd=5.0, cost_model="subscription"))
        dispatcher.emit(_make_task_scored(cost_usd=3.0, cost_model="unknown"))
        dispatcher.shutdown()

        assert checker.cumulative_cost == pytest.approx(0.0)

    def test_ignores_none_cost(self) -> None:
        checker = BudgetChecker(budget=10.0)
        dispatcher = EventDispatcher()
        checker.set_dispatcher(dispatcher)
        dispatcher.register(checker)

        dispatcher.emit(_make_task_scored(cost_usd=None, cost_model="per_token"))
        dispatcher.shutdown()

        assert checker.cumulative_cost == pytest.approx(0.0)

    def test_exceeded_at_budget(self) -> None:
        checker = BudgetChecker(budget=5.0)
        dispatcher = EventDispatcher()
        checker.set_dispatcher(dispatcher)
        dispatcher.register(checker)

        dispatcher.emit(_make_task_scored(cost_usd=5.0))
        dispatcher.shutdown()

        assert checker.is_exceeded is True

    def test_not_exceeded_below_budget(self) -> None:
        checker = BudgetChecker(budget=10.0)
        dispatcher = EventDispatcher()
        checker.set_dispatcher(dispatcher)
        dispatcher.register(checker)

        dispatcher.emit(_make_task_scored(cost_usd=7.0))
        dispatcher.shutdown()

        assert checker.is_exceeded is False

    def test_emits_warning_at_80_percent(self) -> None:
        warning_listener = RecordingListener()
        checker = BudgetChecker(budget=10.0, warning_threshold=0.8)
        dispatcher = EventDispatcher()
        checker.set_dispatcher(dispatcher)
        dispatcher.register(checker)
        dispatcher.register(warning_listener)

        # 8.0 >= 10.0 * 0.8 → should trigger warning
        dispatcher.emit(_make_task_scored(cost_usd=8.0))
        dispatcher.shutdown()

        warnings = [e for e in warning_listener.events if isinstance(e, BudgetWarning)]
        assert len(warnings) == 1
        assert warnings[0].threshold_pct == 0.8
        assert warnings[0].cumulative_cost == pytest.approx(8.0)

    def test_emits_warning_at_100_percent(self) -> None:
        warning_listener = RecordingListener()
        checker = BudgetChecker(budget=5.0, warning_threshold=0.8)
        dispatcher = EventDispatcher()
        checker.set_dispatcher(dispatcher)
        dispatcher.register(checker)
        dispatcher.register(warning_listener)

        # First event: 5.0 >= 5.0 * 0.8 (80%) AND 5.0 >= 5.0 (100%)
        dispatcher.emit(_make_task_scored(cost_usd=5.0))
        dispatcher.shutdown()

        warnings = [e for e in warning_listener.events if isinstance(e, BudgetWarning)]
        # Both 80% and 100% warnings emitted
        assert len(warnings) == 2
        thresholds = sorted(w.threshold_pct for w in warnings)
        assert thresholds == [0.8, 1.0]

    def test_warning_emitted_only_once_per_threshold(self) -> None:
        warning_listener = RecordingListener()
        checker = BudgetChecker(budget=10.0, warning_threshold=0.8)
        dispatcher = EventDispatcher()
        checker.set_dispatcher(dispatcher)
        dispatcher.register(checker)
        dispatcher.register(warning_listener)

        # Cross 80% twice — should only emit once
        dispatcher.emit(_make_task_scored(cost_usd=8.5))
        dispatcher.emit(_make_task_scored(cost_usd=0.5))
        dispatcher.shutdown()

        warnings = [e for e in warning_listener.events if isinstance(e, BudgetWarning)]
        pct_80 = [w for w in warnings if w.threshold_pct == 0.8]
        assert len(pct_80) == 1
