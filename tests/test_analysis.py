"""Tests for the analysis module."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codeprobe.analysis import (
    ConfigSummary,
    Report,
    compare_configs,
    format_json_report,
    format_text_report,
    generate_report,
    rank_configs,
    summarize_config,
)
from codeprobe.analysis.report import generate_report_streaming
from codeprobe.analysis.stats import summarize_completed_tasks
from codeprobe.models.experiment import CompletedTask, ConfigResults

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    task_id: str,
    score: float,
    *,
    status: str = "completed",
    duration: float = 10.0,
    cost: float | None = None,
    tokens: int | None = None,
) -> CompletedTask:
    return CompletedTask(
        task_id=task_id,
        automated_score=score,
        status=status,
        duration_seconds=duration,
        cost_usd=cost,
        token_count=tokens,
    )


# ---------------------------------------------------------------------------
# summarize_config
# ---------------------------------------------------------------------------


class TestSummarizeConfig:
    def test_basic(self) -> None:
        """3 tasks, mix of scores, verify all summary fields."""
        results = ConfigResults(
            config="baseline",
            completed=[
                _task("t1", 1.0, duration=10.0),
                _task("t2", 0.5, duration=20.0),
                _task("t3", 0.0, duration=30.0),
            ],
        )
        s = summarize_config(results)

        assert s.label == "baseline"
        assert s.total_tasks == 3
        assert s.completed == 3
        assert s.errored == 0
        assert s.pass_rate == pytest.approx(2 / 3)
        assert s.mean_score == pytest.approx(0.5)
        assert s.median_score == pytest.approx(0.5)
        assert s.total_duration_sec == pytest.approx(60.0)
        assert s.mean_duration_sec == pytest.approx(20.0)
        assert s.total_cost_usd is None
        assert s.total_tokens is None

    def test_empty(self) -> None:
        """No tasks produce zeros."""
        results = ConfigResults(config="empty", completed=[])
        s = summarize_config(results)

        assert s.label == "empty"
        assert s.total_tasks == 0
        assert s.completed == 0
        assert s.errored == 0
        assert s.pass_rate == 0.0
        assert s.mean_score == 0.0
        assert s.median_score == 0.0
        assert s.total_duration_sec == 0.0
        assert s.mean_duration_sec == 0.0
        assert s.total_cost_usd is None
        assert s.total_tokens is None

    def test_with_costs(self) -> None:
        """Tasks with cost_usd and token_count are aggregated."""
        results = ConfigResults(
            config="expensive",
            completed=[
                _task("t1", 1.0, duration=5.0, cost=0.10, tokens=500),
                _task("t2", 0.8, duration=8.0, cost=0.20, tokens=1000),
                _task("t3", 0.6, duration=7.0, cost=0.12, tokens=750),
            ],
        )
        s = summarize_config(results)

        assert s.total_cost_usd == pytest.approx(0.42)
        assert s.total_tokens == 2250
        assert s.pass_rate == pytest.approx(1.0)

    def test_errored_tasks(self) -> None:
        """Tasks with non-completed status count as errored."""
        results = ConfigResults(
            config="mixed",
            completed=[
                _task("t1", 1.0),
                _task("t2", 0.0, status="error"),
            ],
        )
        s = summarize_config(results)

        assert s.total_tasks == 2
        assert s.completed == 1
        assert s.errored == 1


# ---------------------------------------------------------------------------
# compare_configs
# ---------------------------------------------------------------------------


class TestCompareConfigs:
    def test_clear_winner(self) -> None:
        """One config clearly better in all dimensions."""
        a = ConfigSummary(
            label="good",
            total_tasks=3,
            completed=3,
            errored=0,
            pass_rate=1.0,
            mean_score=0.9,
            median_score=0.9,
            total_duration_sec=30.0,
            mean_duration_sec=10.0,
            total_cost_usd=0.30,
            total_tokens=1500,
        )
        b = ConfigSummary(
            label="bad",
            total_tasks=3,
            completed=2,
            errored=1,
            pass_rate=0.5,
            mean_score=0.4,
            median_score=0.4,
            total_duration_sec=60.0,
            mean_duration_sec=20.0,
            total_cost_usd=0.50,
            total_tokens=2500,
        )
        cmp = compare_configs(a, b)

        assert cmp.config_a == "good"
        assert cmp.config_b == "bad"
        assert cmp.score_diff == pytest.approx(0.5)
        assert cmp.cost_diff == pytest.approx(-0.20)
        assert cmp.speed_diff == pytest.approx(-10.0)
        assert cmp.winner == "good"
        assert "good" in cmp.summary
        assert "bad" in cmp.summary

    def test_cost_tradeoff(self) -> None:
        """One config has better score, other has lower cost."""
        a = ConfigSummary(
            label="accurate",
            total_tasks=3,
            completed=3,
            errored=0,
            pass_rate=0.9,
            mean_score=0.85,
            median_score=0.85,
            total_duration_sec=45.0,
            mean_duration_sec=15.0,
            total_cost_usd=0.60,
            total_tokens=3000,
        )
        b = ConfigSummary(
            label="cheap",
            total_tasks=3,
            completed=3,
            errored=0,
            pass_rate=0.7,
            mean_score=0.70,
            median_score=0.70,
            total_duration_sec=30.0,
            mean_duration_sec=10.0,
            total_cost_usd=0.20,
            total_tokens=1000,
        )
        cmp = compare_configs(a, b)

        # Score wins: accurate is the winner
        assert cmp.winner == "accurate"
        assert cmp.score_diff == pytest.approx(0.15)
        assert cmp.cost_diff == pytest.approx(0.40)

    def test_same_score_cost_wins(self) -> None:
        """When scores are equal, lower cost wins."""
        base = dict(
            total_tasks=3,
            completed=3,
            errored=0,
            pass_rate=1.0,
            mean_score=0.8,
            median_score=0.8,
            total_duration_sec=30.0,
            mean_duration_sec=10.0,
            total_tokens=1000,
        )
        a = ConfigSummary(label="a", total_cost_usd=0.50, **base)
        b = ConfigSummary(label="b", total_cost_usd=0.30, **base)
        cmp = compare_configs(a, b)

        assert cmp.winner == "b"


# ---------------------------------------------------------------------------
# rank_configs
# ---------------------------------------------------------------------------


class TestRankConfigs:
    def test_single(self) -> None:
        """Single config gets rank 1."""
        s = ConfigSummary(
            label="only",
            total_tasks=5,
            completed=5,
            errored=0,
            pass_rate=0.8,
            mean_score=0.75,
            median_score=0.8,
            total_duration_sec=50.0,
            mean_duration_sec=10.0,
            total_cost_usd=0.25,
            total_tokens=2000,
        )
        ranked = rank_configs([s])

        assert len(ranked) == 1
        assert ranked[0].rank == 1
        assert ranked[0].label == "only"
        assert "Best overall" in ranked[0].recommendation

    def test_multiple(self) -> None:
        """3 configs ranked correctly by score."""
        high = ConfigSummary(
            label="high",
            total_tasks=5,
            completed=5,
            errored=0,
            pass_rate=1.0,
            mean_score=0.9,
            median_score=0.9,
            total_duration_sec=50.0,
            mean_duration_sec=10.0,
            total_cost_usd=0.50,
            total_tokens=2500,
        )
        mid = ConfigSummary(
            label="mid",
            total_tasks=5,
            completed=4,
            errored=1,
            pass_rate=0.6,
            mean_score=0.5,
            median_score=0.5,
            total_duration_sec=40.0,
            mean_duration_sec=8.0,
            total_cost_usd=0.30,
            total_tokens=1500,
        )
        low = ConfigSummary(
            label="low",
            total_tasks=5,
            completed=2,
            errored=3,
            pass_rate=0.0,
            mean_score=0.0,
            median_score=0.0,
            total_duration_sec=60.0,
            mean_duration_sec=12.0,
            total_cost_usd=0.10,
            total_tokens=500,
        )
        ranked = rank_configs([mid, low, high])

        assert ranked[0].rank == 1
        assert ranked[0].label == "high"
        assert "Best overall" in ranked[0].recommendation

        assert ranked[1].rank == 2
        assert ranked[1].label == "mid"

        assert ranked[2].rank == 3
        assert ranked[2].label == "low"
        assert "Not recommended" in ranked[2].recommendation

    def test_empty(self) -> None:
        """Empty list returns empty."""
        assert rank_configs([]) == []

    def test_cost_efficiency_recommendation(self) -> None:
        """Cheapest config within 10% of best score gets cost-efficiency tag."""
        best = ConfigSummary(
            label="best",
            total_tasks=5,
            completed=5,
            errored=0,
            pass_rate=1.0,
            mean_score=0.90,
            median_score=0.90,
            total_duration_sec=50.0,
            mean_duration_sec=10.0,
            total_cost_usd=1.00,
            total_tokens=5000,
        )
        cheap = ConfigSummary(
            label="cheap",
            total_tasks=5,
            completed=5,
            errored=0,
            pass_rate=0.9,
            mean_score=0.85,  # within 10% of 0.90
            median_score=0.85,
            total_duration_sec=40.0,
            mean_duration_sec=8.0,
            total_cost_usd=0.20,
            total_tokens=1000,
        )
        ranked = rank_configs([best, cheap])

        assert ranked[0].label == "best"
        assert ranked[1].label == "cheap"
        assert "cost-efficiency" in ranked[1].recommendation.lower()


# ---------------------------------------------------------------------------
# generate_report
# ---------------------------------------------------------------------------


class TestGenerateReport:
    def test_full_pipeline(self) -> None:
        """Full pipeline from ConfigResults to Report."""
        results_a = ConfigResults(
            config="config-a",
            completed=[
                _task("t1", 1.0, duration=10.0, cost=0.10, tokens=500),
                _task("t2", 0.7, duration=15.0, cost=0.12, tokens=600),
            ],
        )
        results_b = ConfigResults(
            config="config-b",
            completed=[
                _task("t1", 0.5, duration=8.0, cost=0.05, tokens=300),
                _task("t2", 0.3, duration=12.0, cost=0.09, tokens=400),
            ],
        )

        report = generate_report("my-experiment", [results_a, results_b])

        assert isinstance(report, Report)
        assert report.experiment_name == "my-experiment"
        assert len(report.summaries) == 2
        assert len(report.rankings) == 2
        assert len(report.comparisons) == 1

        assert report.rankings[0].label == "config-a"
        assert report.comparisons[0].winner == "config-a"


# ---------------------------------------------------------------------------
# format_text_report
# ---------------------------------------------------------------------------


class TestFormatTextReport:
    def test_contains_key_sections(self) -> None:
        """Verify text output contains key sections."""
        results = ConfigResults(
            config="alpha",
            completed=[
                _task("t1", 1.0, duration=10.0, cost=0.20),
                _task("t2", 0.8, duration=15.0, cost=0.22),
            ],
        )
        report = generate_report("test-exp", [results])
        text = format_text_report(report)

        assert "## Experiment: test-exp" in text
        assert "### Rankings" in text
        assert "### Recommendation" in text
        assert "alpha" in text
        assert "pass rate" in text


# ---------------------------------------------------------------------------
# format_json_report
# ---------------------------------------------------------------------------


class TestFormatJsonReport:
    def test_valid_json_with_expected_keys(self) -> None:
        """Verify valid JSON with expected keys."""
        results = ConfigResults(
            config="beta",
            completed=[
                _task("t1", 0.9, duration=12.0, cost=0.15, tokens=800),
            ],
        )
        report = generate_report("json-exp", [results])
        text = format_json_report(report)

        data = json.loads(text)
        assert data["experiment_name"] == "json-exp"
        assert "summaries" in data
        assert "rankings" in data
        assert "comparisons" in data

        assert len(data["summaries"]) == 1
        assert data["summaries"][0]["label"] == "beta"
        assert data["rankings"][0]["rank"] == 1

    def test_multiple_configs_json(self) -> None:
        """Multiple configs produce correct JSON structure."""
        results_a = ConfigResults(
            config="a",
            completed=[_task("t1", 1.0, duration=10.0)],
        )
        results_b = ConfigResults(
            config="b",
            completed=[_task("t1", 0.5, duration=20.0)],
        )
        report = generate_report("multi", [results_a, results_b])
        data = json.loads(format_json_report(report))

        assert len(data["summaries"]) == 2
        assert len(data["rankings"]) == 2
        assert len(data["comparisons"]) == 1


# ---------------------------------------------------------------------------
# summarize_completed_tasks (streaming)
# ---------------------------------------------------------------------------


class TestSummarizeCompletedTasks:
    def test_matches_batch(self) -> None:
        """Streaming summarize produces identical output to batch summarize."""
        tasks = [
            _task("t1", 1.0, duration=10.0, cost=0.10, tokens=500),
            _task("t2", 0.5, duration=20.0, cost=0.20, tokens=1000),
            _task("t3", 0.0, duration=30.0),
        ]
        batch_result = summarize_config(ConfigResults(config="test", completed=tasks))
        stream_result = summarize_completed_tasks("test", iter(tasks))

        assert stream_result == batch_result

    def test_empty_iterator(self) -> None:
        """Empty iterator produces zero summary."""
        result = summarize_completed_tasks("empty", iter([]))
        batch = summarize_config(ConfigResults(config="empty", completed=[]))
        assert result == batch

    def test_single_pass(self) -> None:
        """Verify the iterator is consumed exactly once (no rewind)."""

        class OnceIterator:
            """Iterator that raises on second iteration attempt."""

            def __init__(self, items: list[CompletedTask]) -> None:
                self._iter = iter(items)
                self._exhausted = False

            def __iter__(self) -> Iterator[CompletedTask]:
                if self._exhausted:
                    raise RuntimeError("Iterator consumed twice")
                return self

            def __next__(self) -> CompletedTask:
                try:
                    return next(self._iter)
                except StopIteration:
                    self._exhausted = True
                    raise

        tasks = [_task("t1", 1.0, duration=5.0)]
        result = summarize_completed_tasks("once", OnceIterator(tasks))
        assert result.total_tasks == 1

    def test_large_synthetic(self) -> None:
        """10K synthetic tasks produce identical results streamed vs batch."""
        tasks = [
            _task(
                f"t{i}",
                score=(i % 3) / 2.0,
                duration=float(i % 50),
                cost=0.01 * (i % 10) if i % 5 != 0 else None,
                tokens=100 * (i % 20) if i % 7 != 0 else None,
            )
            for i in range(10_000)
        ]
        batch = summarize_config(ConfigResults(config="big", completed=tasks))
        stream = summarize_completed_tasks("big", iter(tasks))
        assert stream == batch


# ---------------------------------------------------------------------------
# generate_report_streaming
# ---------------------------------------------------------------------------


class TestGenerateReportStreaming:
    def test_matches_batch(self) -> None:
        """Streaming report matches batch report exactly."""
        results_a = ConfigResults(
            config="config-a",
            completed=[
                _task("t1", 1.0, duration=10.0, cost=0.10, tokens=500),
                _task("t2", 0.7, duration=15.0, cost=0.12, tokens=600),
            ],
        )
        results_b = ConfigResults(
            config="config-b",
            completed=[
                _task("t1", 0.5, duration=8.0, cost=0.05, tokens=300),
                _task("t2", 0.3, duration=12.0, cost=0.09, tokens=400),
            ],
        )
        batch_report = generate_report("test", [results_a, results_b])

        def stream_pairs() -> Iterator[tuple[str, Iterator[CompletedTask]]]:
            yield ("config-a", iter(results_a.completed))
            yield ("config-b", iter(results_b.completed))

        stream_report = generate_report_streaming("test", stream_pairs())

        assert stream_report.summaries == batch_report.summaries
        assert stream_report.rankings == batch_report.rankings
        assert stream_report.comparisons == batch_report.comparisons

    def test_empty_configs(self) -> None:
        """No configs produces empty report."""
        report = generate_report_streaming("empty", iter([]))
        assert report.summaries == ()
        assert report.rankings == ()
        assert report.comparisons == ()

    def test_single_config(self) -> None:
        """Single config streaming produces valid report."""
        tasks = [_task("t1", 0.9, duration=5.0)]

        def stream() -> Iterator[tuple[str, Iterator[CompletedTask]]]:
            yield ("solo", iter(tasks))

        report = generate_report_streaming("solo-exp", stream())
        assert len(report.summaries) == 1
        assert len(report.rankings) == 1
        assert report.rankings[0].label == "solo"


# ---------------------------------------------------------------------------
# Partial results: ConfigSummary fields
# ---------------------------------------------------------------------------


class TestConfigSummaryPartialFields:
    def test_defaults_not_partial(self) -> None:
        """ConfigSummary defaults to is_partial=False, tasks_expected=None."""
        results = ConfigResults(
            config="full",
            completed=[_task("t1", 1.0, duration=5.0)],
        )
        s = summarize_config(results)
        assert s.is_partial is False
        assert s.tasks_expected is None

    def test_streaming_defaults_not_partial(self) -> None:
        """summarize_completed_tasks defaults to is_partial=False."""
        tasks = [_task("t1", 1.0, duration=5.0)]
        s = summarize_completed_tasks("test", iter(tasks))
        assert s.is_partial is False
        assert s.tasks_expected is None

    def test_streaming_with_total_tasks(self) -> None:
        """When total_tasks > completed, summary is marked partial."""
        tasks = [_task("t1", 1.0, duration=5.0), _task("t2", 0.8, duration=3.0)]
        s = summarize_completed_tasks("test", iter(tasks), total_tasks=5)
        assert s.is_partial is True
        assert s.tasks_expected == 5
        assert s.total_tasks == 2

    def test_streaming_complete_when_all_done(self) -> None:
        """When total_tasks == completed, summary is NOT partial."""
        tasks = [_task("t1", 1.0, duration=5.0), _task("t2", 0.8, duration=3.0)]
        s = summarize_completed_tasks("test", iter(tasks), total_tasks=2)
        assert s.is_partial is False
        assert s.tasks_expected == 2

    def test_summarize_config_with_total_tasks(self) -> None:
        """summarize_config also accepts total_tasks."""
        results = ConfigResults(
            config="partial",
            completed=[_task("t1", 1.0, duration=5.0)],
        )
        s = summarize_config(results, total_tasks=10)
        assert s.is_partial is True
        assert s.tasks_expected == 10


# ---------------------------------------------------------------------------
# Partial results: Report metadata
# ---------------------------------------------------------------------------


class TestPartialReport:
    def test_report_not_partial_by_default(self) -> None:
        """Report without total_tasks is not partial."""
        results = ConfigResults(
            config="full",
            completed=[_task("t1", 1.0, duration=5.0)],
        )
        report = generate_report("test", [results])
        assert report.is_partial is False
        assert report.completion_ratio is None
        assert report.tasks_expected is None

    def test_report_partial_with_total_tasks(self) -> None:
        """Report with total_tasks > completed is partial."""
        results = ConfigResults(
            config="partial",
            completed=[_task("t1", 1.0, duration=5.0)],
        )
        report = generate_report("test", [results], total_tasks=5)
        assert report.is_partial is True
        assert report.tasks_expected == 5
        assert report.completion_ratio == pytest.approx(0.2)

    def test_report_complete_when_all_done(self) -> None:
        """Report where tasks == total_tasks is not partial."""
        results = ConfigResults(
            config="done",
            completed=[_task("t1", 1.0, duration=5.0)],
        )
        report = generate_report("test", [results], total_tasks=1)
        assert report.is_partial is False
        assert report.tasks_expected == 1
        assert report.completion_ratio == pytest.approx(1.0)

    def test_streaming_report_partial(self) -> None:
        """Streaming report also supports partial metadata."""
        tasks = [_task("t1", 1.0, duration=5.0)]

        def stream() -> Iterator[tuple[str, Iterator[CompletedTask]]]:
            yield ("cfg", iter(tasks))

        report = generate_report_streaming("test", stream(), total_tasks=10)
        assert report.is_partial is True
        assert report.tasks_expected == 10
        assert report.completion_ratio == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Partial results: text format
# ---------------------------------------------------------------------------


class TestPartialTextReport:
    def test_partial_header_shown(self) -> None:
        """Partial report text includes N/M tasks (X%) header."""
        results = ConfigResults(
            config="alpha",
            completed=[
                _task("t1", 1.0, duration=10.0, cost=0.20),
                _task("t2", 0.8, duration=15.0, cost=0.22),
            ],
        )
        report = generate_report("test-exp", [results], total_tasks=10)
        text = format_text_report(report)

        assert "2/10 tasks (20%)" in text
        assert "PARTIAL" in text

    def test_complete_report_no_partial_header(self) -> None:
        """Complete report does not show partial header."""
        results = ConfigResults(
            config="alpha",
            completed=[_task("t1", 1.0, duration=10.0)],
        )
        report = generate_report("test-exp", [results])
        text = format_text_report(report)

        assert "PARTIAL" not in text


# ---------------------------------------------------------------------------
# Partial results: JSON format
# ---------------------------------------------------------------------------


class TestPartialJsonReport:
    def test_partial_metadata_in_json(self) -> None:
        """Partial report JSON includes partial metadata."""
        results = ConfigResults(
            config="beta",
            completed=[_task("t1", 0.9, duration=12.0, cost=0.15, tokens=800)],
        )
        report = generate_report("json-exp", [results], total_tasks=5)
        text = format_json_report(report)
        data = json.loads(text)

        assert data["is_partial"] is True
        assert data["tasks_expected"] == 5
        assert data["completion_ratio"] == pytest.approx(0.2)

    def test_complete_json_metadata(self) -> None:
        """Complete report JSON has is_partial=False."""
        results = ConfigResults(
            config="beta",
            completed=[_task("t1", 0.9, duration=12.0)],
        )
        report = generate_report("json-exp", [results])
        text = format_json_report(report)
        data = json.loads(text)

        assert data["is_partial"] is False
        assert data["tasks_expected"] is None
        assert data["completion_ratio"] is None


# ---------------------------------------------------------------------------
# interpret_cmd: incomplete sweep detection
# ---------------------------------------------------------------------------


class TestInterpretPartialDetection:
    """Test that interpret_cmd detects incomplete sweeps."""

    def test_detects_partial_from_checkpoint(self, tmp_path: Path) -> None:
        """When checkpoint has fewer tasks than manifest, report is partial."""
        from codeprobe.cli.interpret_cmd import _count_expected_tasks

        # Create a tasks directory with 5 task subdirs
        tasks_dir = tmp_path / "tasks"
        for i in range(5):
            task_dir = tasks_dir / f"task-{i}"
            task_dir.mkdir(parents=True)
            (task_dir / "instruction.md").write_text(f"Task {i}")

        count = _count_expected_tasks(tasks_dir)
        assert count == 5

    def test_no_tasks_dir_returns_none(self, tmp_path: Path) -> None:
        """Missing tasks directory returns None."""
        from codeprobe.cli.interpret_cmd import _count_expected_tasks

        count = _count_expected_tasks(tmp_path / "nonexistent")
        assert count is None
