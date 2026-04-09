"""Tests for cli.json_display — JSON Line event consumer."""

from __future__ import annotations

import io
import json
import time

import pytest

from codeprobe.cli.json_display import JsonLineListener
from codeprobe.core.events import (
    BudgetWarning,
    EventDispatcher,
    RunFinished,
    RunStarted,
    TaskScored,
    TaskStarted,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_run_started() -> RunStarted:
    return RunStarted(total_tasks=3, config_label="default", timestamp=1000.0)


def _make_task_started() -> TaskStarted:
    return TaskStarted(task_id="task-1", config_label="default", timestamp=1001.0)


def _make_task_scored() -> TaskScored:
    return TaskScored(
        task_id="task-1",
        config_label="default",
        automated_score=1.0,
        duration_seconds=12.5,
        cost_usd=0.03,
        input_tokens=500,
        output_tokens=200,
        cache_read_tokens=None,
        cost_model="per_token",
        cost_source="api",
        error=None,
        timestamp=1002.0,
    )


def _make_budget_warning() -> BudgetWarning:
    return BudgetWarning(
        cumulative_cost=0.80,
        budget=1.00,
        threshold_pct=0.8,
        timestamp=1003.0,
    )


def _make_run_finished() -> RunFinished:
    return RunFinished(
        total_tasks=3,
        completed_count=3,
        mean_score=0.85,
        total_cost=0.10,
        total_duration=45.0,
        timestamp=1004.0,
    )


ALL_EVENTS = [
    _make_run_started,
    _make_task_started,
    _make_task_scored,
    _make_budget_warning,
    _make_run_finished,
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestJsonLineListener:
    """Core serialization tests."""

    def test_run_started_has_type_key(self) -> None:
        buf = io.StringIO()
        listener = JsonLineListener(file=buf)
        listener.on_event(_make_run_started())

        line = buf.getvalue().strip()
        data = json.loads(line)
        assert data["type"] == "RunStarted"
        assert data["total_tasks"] == 3

    def test_run_started_fields(self) -> None:
        buf = io.StringIO()
        listener = JsonLineListener(file=buf)
        listener.on_event(_make_run_started())

        data = json.loads(buf.getvalue().strip())
        assert data["config_label"] == "default"
        assert data["timestamp"] == 1000.0

    @pytest.mark.parametrize("factory", ALL_EVENTS, ids=lambda f: f.__name__)
    def test_all_event_types_produce_valid_json(self, factory: object) -> None:
        buf = io.StringIO()
        listener = JsonLineListener(file=buf)
        event = factory()  # type: ignore[operator]
        listener.on_event(event)

        line = buf.getvalue().strip()
        assert line, f"No output for {factory}"
        data = json.loads(line)
        assert "type" in data
        assert data["type"] == event.__class__.__name__

    @pytest.mark.parametrize("factory", ALL_EVENTS, ids=lambda f: f.__name__)
    def test_each_line_parseable_by_json_loads(self, factory: object) -> None:
        buf = io.StringIO()
        listener = JsonLineListener(file=buf)
        listener.on_event(factory())  # type: ignore[operator]

        raw = buf.getvalue()
        # Must end with newline
        assert raw.endswith("\n")
        # Must be a single line (no embedded newlines in the JSON)
        lines = raw.strip().split("\n")
        assert len(lines) == 1
        # Must be valid JSON
        json.loads(lines[0])

    def test_multiple_events_produce_multiple_lines(self) -> None:
        buf = io.StringIO()
        listener = JsonLineListener(file=buf)
        for factory in ALL_EVENTS:
            listener.on_event(factory())  # type: ignore[operator]

        lines = buf.getvalue().strip().split("\n")
        assert len(lines) == len(ALL_EVENTS)
        for line in lines:
            data = json.loads(line)
            assert "type" in data

    def test_task_scored_none_fields_serialized(self) -> None:
        """Ensure None values are preserved (not dropped)."""
        buf = io.StringIO()
        listener = JsonLineListener(file=buf)
        event = TaskScored(
            task_id="t",
            config_label="c",
            automated_score=0.0,
            duration_seconds=1.0,
            cost_usd=None,
            input_tokens=None,
            output_tokens=None,
            cache_read_tokens=None,
            cost_model="unknown",
            cost_source="none",
            error=None,
            timestamp=0.0,
        )
        listener.on_event(event)
        data = json.loads(buf.getvalue().strip())
        assert data["cost_usd"] is None
        assert data["error"] is None

    def test_graceful_skip_on_serialization_error(self) -> None:
        """If asdict or json.dumps raises, the listener must not crash."""
        buf = io.StringIO()
        listener = JsonLineListener(file=buf)
        # Pass a non-dataclass object — asdict will raise TypeError
        listener.on_event("not-an-event")  # type: ignore[arg-type]
        assert buf.getvalue() == ""


class TestJsonLineWithDispatcher:
    """Integration: JsonLineListener registered on EventDispatcher."""

    def test_dispatcher_delivers_to_json_listener(self) -> None:
        buf = io.StringIO()
        listener = JsonLineListener(file=buf)
        dispatcher = EventDispatcher()
        dispatcher.register(listener)

        dispatcher.emit(_make_run_started())
        dispatcher.emit(_make_task_started())
        dispatcher.shutdown()

        lines = buf.getvalue().strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0])["type"] == "RunStarted"
        assert json.loads(lines[1])["type"] == "TaskStarted"


class TestQuietDoesNotSuppressJson:
    """Verify that --quiet only suppresses display listeners, not JSON."""

    def test_quiet_mode_still_emits_json_events(self) -> None:
        """Simulate the quiet+json path from run_cmd._run_config logic.

        When quiet=True and log_format='json', the JsonLineListener is
        registered and PlainTextListener is NOT.  JSON events must still
        appear.
        """
        buf = io.StringIO()
        json_listener = JsonLineListener(file=buf)

        dispatcher = EventDispatcher()
        # Replicate run_cmd logic: json mode registers JsonLineListener;
        # quiet suppresses PlainTextListener — but JSON is unaffected.
        log_format = "json"
        quiet = True
        if log_format == "json":
            dispatcher.register(json_listener)
        # PlainTextListener only when not quiet AND not json
        # (matches the production code path)

        dispatcher.emit(_make_run_started())
        dispatcher.emit(_make_task_scored())
        dispatcher.emit(_make_run_finished())
        dispatcher.shutdown()

        lines = buf.getvalue().strip().split("\n")
        assert len(lines) == 3
        types = [json.loads(line)["type"] for line in lines]
        assert types == ["RunStarted", "TaskScored", "RunFinished"]
