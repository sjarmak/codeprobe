"""Tests for dual-verifier scoring display in run listeners.

Verifies that ``PlainTextListener`` and ``RichLiveListener`` render the
optional ``TaskScored.scoring_details`` payload as a
``(code:PASS|FAIL artifact:0.XX)`` suffix, and fall back to the legacy
single-score format when ``scoring_details`` is ``None``.
"""

from __future__ import annotations

import io
import json
import time

import pytest
from rich.console import Console

from codeprobe.cli.rich_display import RichLiveListener
from codeprobe.cli.run_cmd import NdjsonStdoutListener, PlainTextListener
from codeprobe.core.events import RunFinished, RunStarted, TaskScored


def _make_task_scored(
    *,
    task_id: str = "t1",
    automated_score: float = 0.7,
    scoring_details: dict | None = None,
    verdict: str | None = None,
    status: str = "completed",
    error_category: str | None = None,
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
        cache_creation_tokens=0,
        cost_model="per_token",
        cost_source="api",
        error=None,
        timestamp=time.time(),
        scoring_details=scoring_details,
        verdict=verdict,
        status=status,
        error_category=error_category,
    )


# ---------------------------------------------------------------------------
# PlainTextListener
# ---------------------------------------------------------------------------


class TestPlainTextListenerDual:
    def test_verifier_error_uses_infra_status(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        PlainTextListener().on_event(
            _make_task_scored(
                task_id="broken",
                automated_score=0.0,
                scoring_details={"passed": False, "verdict": "verifier_error"},
            )
        )

        output = capsys.readouterr().out
        assert "broken: INFRA" in output
        assert "FAIL" not in output

    def test_top_level_verdict_takes_precedence_over_nested_value(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        event = _make_task_scored(
            task_id="incorrect",
            automated_score=0.0,
            verdict="incorrect",
            scoring_details={"verdict": "verifier_error"},
        )

        PlainTextListener().on_event(event)
        NdjsonStdoutListener().on_event(event)

        lines = capsys.readouterr().out.splitlines()
        assert "incorrect: FAIL" in lines[0]
        payload = json.loads(lines[1])
        assert payload["verdict"] == "incorrect"
        assert payload["outcome"] == "scored"

    def test_ndjson_nested_verifier_error_is_infra_failure(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        NdjsonStdoutListener().on_event(
            _make_task_scored(
                task_id="broken",
                automated_score=0.0,
                scoring_details={"verdict": "verifier_error"},
            )
        )

        payload = json.loads(capsys.readouterr().out)
        assert payload["verdict"] == "verifier_error"
        assert payload["outcome"] == "infra_failure"

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
    @pytest.mark.parametrize("error_category", ["quota", "timeout", "system"])
    @pytest.mark.parametrize("status", ["completed", "error"])
    def test_infrastructure_errors_use_infra_status_and_denominator(
        self,
        error_category: str,
        status: str,
    ) -> None:
        listener, _ = _make_rich_listener()
        _start_run(listener, total=1)
        try:
            listener.on_event(
                _make_task_scored(
                    task_id="broken",
                    automated_score=0.0,
                    status=status,
                    error_category=error_category,
                )
            )
            rendered = _render_display_to_str(listener)
        finally:
            if listener._live is not None:  # type: ignore[attr-defined]
                listener._live.stop()  # type: ignore[attr-defined]

        assert "INFRA" in rendered
        assert "0 scored, 1 infra" in rendered

    def test_verifier_error_uses_infra_status_and_denominator(self) -> None:
        listener, _ = _make_rich_listener()
        _start_run(listener, total=1)
        try:
            listener.on_event(
                _make_task_scored(
                    task_id="broken",
                    automated_score=0.0,
                    scoring_details={"passed": False, "verdict": "verifier_error"},
                )
            )
            rendered = _render_display_to_str(listener)
        finally:
            if listener._live is not None:  # type: ignore[attr-defined]
                listener._live.stop()  # type: ignore[attr-defined]

        assert "INFRA" in rendered
        assert "0 scored, 1 infra" in rendered

    def test_finished_summary_uses_run_finished_reward_population(self) -> None:
        listener, output = _make_rich_listener()
        _start_run(listener, total=2)
        listener.on_event(
            _make_task_scored(
                task_id="broken",
                automated_score=0.0,
                verdict="verifier_error",
                scoring_details={"passed": False, "verdict": "verifier_error"},
            )
        )
        listener.on_event(
            _make_task_scored(
                task_id="correct",
                automated_score=1.0,
                verdict="correct",
                scoring_details={"passed": True, "verdict": "correct"},
            )
        )
        listener.on_event(
            RunFinished(
                total_tasks=2,
                completed_count=2,
                mean_score=1.0,
                total_cost=0.02,
                total_duration=3.0,
                config_label="cfg",
                timestamp=time.time(),
                scored_count=1,
                infra_failure_count=1,
            )
        )

        rendered = output.getvalue()
        assert "mean score 1.00" in rendered
        assert "1 scored" in rendered
        assert "1 infra" in rendered

    def test_finished_summary_derives_legacy_scored_denominator(self) -> None:
        listener, output = _make_rich_listener()
        _start_run(listener, total=2)
        listener.on_event(_make_task_scored(task_id="first", automated_score=1.0))
        listener.on_event(_make_task_scored(task_id="second", automated_score=0.0))
        listener.on_event(
            RunFinished(
                total_tasks=2,
                completed_count=2,
                mean_score=0.5,
                total_cost=0.02,
                total_duration=3.0,
                config_label="cfg",
                timestamp=time.time(),
            )
        )

        rendered = output.getvalue()
        assert "2 scored" in rendered
        assert "0 infra" in rendered

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
        cache_creation_tokens=0,
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
        cache_creation_tokens=0,
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


# ---------------------------------------------------------------------------
# RichLiveListener pass-count threshold alignment (codeprobe-kth)
# ---------------------------------------------------------------------------


class TestRichLiveListenerPassThreshold:
    """Live dashboard pass counter must use PASS_THRESHOLD (not ``> 0``)
    to match the final report's ``_task_passed()`` logic."""

    def _get_passed_count(
        self,
        automated_score: float,
        scoring_details: dict | None = None,
    ) -> int:
        listener, _ = _make_rich_listener()
        _start_run(listener, total=1)
        try:
            event = _make_task_scored(
                task_id="threshold-test",
                automated_score=automated_score,
                scoring_details=scoring_details,
            )
            listener.on_event(event)
            state = listener._configs["cfg"]  # type: ignore[attr-defined]
            return state.passed
        finally:
            if listener._live is not None:  # type: ignore[attr-defined]
                listener._live.stop()  # type: ignore[attr-defined]

    def test_partial_score_below_threshold_is_not_passed(self) -> None:
        """Score 0.3 with no scoring_details should NOT count as passed."""
        assert self._get_passed_count(0.3) == 0

    def test_score_at_threshold_is_passed(self) -> None:
        """Score 0.5 (== PASS_THRESHOLD) should count as passed."""
        assert self._get_passed_count(0.5) == 1

    def test_full_score_with_explicit_false_is_not_passed(self) -> None:
        """Score 1.0 but scoring_details={'passed': False} → not passed."""
        assert self._get_passed_count(1.0, {"passed": False}) == 0

    def test_zero_score_with_explicit_true_is_passed(self) -> None:
        """Score 0.0 but scoring_details={'passed': True} → passed."""
        assert self._get_passed_count(0.0, {"passed": True}) == 1

    def test_string_false_in_details_is_not_passed(self) -> None:
        """scoring_details={'passed': 'false'} (JSON round-trip) → not passed."""
        assert self._get_passed_count(1.0, {"passed": "false"}) == 0
