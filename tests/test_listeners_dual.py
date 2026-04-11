"""Tests for dual-verifier scoring display in run listeners.

Verifies that ``PlainTextListener`` and ``RichLiveListener`` render the
optional ``TaskScored.scoring_details`` payload as a
``(code:PASS|FAIL artifact:0.XX)`` suffix, and fall back to the legacy
single-score format when ``scoring_details`` is ``None``.
"""

from __future__ import annotations

import io
import time

import pytest
from rich.console import Console

from codeprobe.cli.rich_display import RichLiveListener
from codeprobe.cli.run_cmd import PlainTextListener
from codeprobe.core.events import RunStarted, TaskScored


def _make_task_scored(
    *,
    task_id: str = "t1",
    automated_score: float = 0.7,
    scoring_details: dict | None = None,
) -> TaskScored:
    return TaskScored(
        task_id=task_id,
        config_label="cfg",
        automated_score=automated_score,
        duration_seconds=1.5,
        cost_usd=0.01,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=0,
        cost_model="per_token",
        cost_source="api",
        error=None,
        timestamp=time.time(),
        scoring_details=scoring_details,
    )


# ---------------------------------------------------------------------------
# PlainTextListener
# ---------------------------------------------------------------------------


class TestPlainTextListenerDual:
    def test_without_scoring_details_uses_legacy_format(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        listener = PlainTextListener()
        listener.on_event(_make_task_scored(task_id="task-a", automated_score=1.0))
        out = capsys.readouterr().out

        assert "task-a" in out
        assert "PASS" in out
        # Legacy format has no dual-score suffix
        assert "code:" not in out
        assert "artifact:" not in out

    def test_with_scoring_details_shows_both_sub_scores(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        listener = PlainTextListener()
        event = _make_task_scored(
            task_id="task-b",
            automated_score=0.70,
            scoring_details={
                "score_direct": 1.0,
                "score_artifact": 0.4,
                "passed_direct": True,
                "passed_artifact": False,
            },
        )
        listener.on_event(event)
        out = capsys.readouterr().out

        assert "task-b" in out
        assert "code:PASS" in out
        assert "artifact:0.40" in out

    def test_passed_direct_false_renders_code_fail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        listener = PlainTextListener()
        event = _make_task_scored(
            scoring_details={
                "score_direct": 0.0,
                "score_artifact": 0.85,
                "passed_direct": False,
                "passed_artifact": True,
            },
        )
        listener.on_event(event)
        out = capsys.readouterr().out

        assert "code:FAIL" in out
        assert "artifact:0.85" in out


# ---------------------------------------------------------------------------
# RichLiveListener
# ---------------------------------------------------------------------------


def _make_rich_listener() -> tuple[RichLiveListener, io.StringIO]:
    """Build a RichLiveListener whose console writes to a StringIO buffer."""
    buf = io.StringIO()
    listener = RichLiveListener()
    # Replace the stderr-bound console with one that captures output without
    # engaging a real terminal.
    listener._console = Console(  # type: ignore[attr-defined]
        file=buf,
        force_terminal=False,
        width=200,
        color_system=None,
    )
    return listener, buf


def _start_run(listener: RichLiveListener, total: int = 2) -> None:
    listener.on_event(
        RunStarted(total_tasks=total, config_label="cfg", timestamp=time.time())
    )


def _render_display_to_str(listener: RichLiveListener) -> str:
    """Render the listener's current display to a plain string."""
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=200, color_system=None)
    console.print(listener._build_display())  # type: ignore[attr-defined]
    return buf.getvalue()


class TestRichLiveListenerDual:
    def test_without_scoring_details_uses_legacy_format(self) -> None:
        listener, _ = _make_rich_listener()
        _start_run(listener)
        try:
            listener.on_event(_make_task_scored(task_id="rich-a", automated_score=1.0))
            rendered = _render_display_to_str(listener)
        finally:
            if listener._live is not None:  # type: ignore[attr-defined]
                listener._live.stop()  # type: ignore[attr-defined]

        assert "rich-a" in rendered
        assert "code:" not in rendered
        assert "artifact:" not in rendered

    def test_with_scoring_details_shows_both_sub_scores(self) -> None:
        listener, _ = _make_rich_listener()
        _start_run(listener)
        try:
            event = _make_task_scored(
                task_id="rich-b",
                automated_score=0.70,
                scoring_details={
                    "score_direct": 1.0,
                    "score_artifact": 0.4,
                    "passed_direct": True,
                    "passed_artifact": False,
                },
            )
            listener.on_event(event)
            rendered = _render_display_to_str(listener)
        finally:
            if listener._live is not None:  # type: ignore[attr-defined]
                listener._live.stop()  # type: ignore[attr-defined]

        assert "rich-b" in rendered
        assert "code:PASS" in rendered
        assert "artifact:0.40" in rendered

    def test_passed_direct_false_renders_code_fail(self) -> None:
        listener, _ = _make_rich_listener()
        _start_run(listener)
        try:
            event = _make_task_scored(
                task_id="rich-c",
                scoring_details={
                    "score_direct": 0.0,
                    "score_artifact": 0.85,
                    "passed_direct": False,
                    "passed_artifact": True,
                },
            )
            listener.on_event(event)
            rendered = _render_display_to_str(listener)
        finally:
            if listener._live is not None:  # type: ignore[attr-defined]
                listener._live.stop()  # type: ignore[attr-defined]

        assert "code:FAIL" in rendered
        assert "artifact:0.85" in rendered


# ---------------------------------------------------------------------------
# TaskScored scoring_details default alignment
# ---------------------------------------------------------------------------


class TestTaskScoredScoringDetailsDefault:
    """TaskScored.scoring_details should default to {} (dict) not None,
    matching CompletedTask.scoring_details default."""

    def test_default_is_empty_dict(self) -> None:
        event = TaskScored(
            task_id="t1",
            config_label="cfg",
            automated_score=1.0,
            duration_seconds=1.0,
            cost_usd=0.01,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=0,
            cost_model="per_token",
            cost_source="api",
            error=None,
            timestamp=time.time(),
            # scoring_details not passed — should default to {}
        )
        assert event.scoring_details == {}
        assert event.scoring_details is not None

    def test_plain_text_listener_handles_empty_dict_scoring_details(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """PlainTextListener should handle scoring_details={} the same as None
        (legacy format, no dual suffix)."""
        listener = PlainTextListener()
        event = TaskScored(
            task_id="task-empty",
            config_label="cfg",
            automated_score=1.0,
            duration_seconds=1.0,
            cost_usd=0.01,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=0,
            cost_model="per_token",
            cost_source="api",
            error=None,
            timestamp=time.time(),
            scoring_details={},
        )
        listener.on_event(event)
        out = capsys.readouterr().out
        assert "task-empty" in out
        assert "PASS" in out
        # Empty dict should NOT trigger dual format
        assert "code:" not in out
        assert "artifact:" not in out
