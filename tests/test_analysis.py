"""Tests for the analysis module."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from codeprobe.analysis import (
    ConfigSummary,
    Report,
    cliffs_delta,
    cohens_d,
    compare_configs,
    format_csv_report,
    format_json_report,
    format_text_report,
    generate_report,
    mcnemars_exact_test,
    rank_configs,
    summarize_config,
    wilcoxon_test,
    wilson_ci,
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
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> CompletedTask:
    return CompletedTask(
        task_id=task_id,
        automated_score=score,
        status=status,
        duration_seconds=duration,
        cost_usd=cost,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
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
        """Tasks with cost_usd and input/output tokens are aggregated."""
        results = ConfigResults(
            config="expensive",
            completed=[
                _task(
                    "t1",
                    1.0,
                    duration=5.0,
                    cost=0.10,
                    input_tokens=400,
                    output_tokens=100,
                ),
                _task(
                    "t2",
                    0.8,
                    duration=8.0,
                    cost=0.20,
                    input_tokens=800,
                    output_tokens=200,
                ),
                _task(
                    "t3",
                    0.6,
                    duration=7.0,
                    cost=0.12,
                    input_tokens=600,
                    output_tokens=150,
                ),
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
                _task("t1", 1.0, duration=10.0, cost=0.10, input_tokens=500),
                _task("t2", 0.7, duration=15.0, cost=0.12, input_tokens=600),
            ],
        )
        results_b = ConfigResults(
            config="config-b",
            completed=[
                _task("t1", 0.5, duration=8.0, cost=0.05, input_tokens=300),
                _task("t2", 0.3, duration=12.0, cost=0.09, input_tokens=400),
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
                _task("t1", 0.9, duration=12.0, cost=0.15, input_tokens=800),
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
            _task("t1", 1.0, duration=10.0, cost=0.10, input_tokens=500),
            _task("t2", 0.5, duration=20.0, cost=0.20, input_tokens=1000),
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
                input_tokens=100 * (i % 20) if i % 7 != 0 else None,
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
                _task("t1", 1.0, duration=10.0, cost=0.10, input_tokens=500),
                _task("t2", 0.7, duration=15.0, cost=0.12, input_tokens=600),
            ],
        )
        results_b = ConfigResults(
            config="config-b",
            completed=[
                _task("t1", 0.5, duration=8.0, cost=0.05, input_tokens=300),
                _task("t2", 0.3, duration=12.0, cost=0.09, input_tokens=400),
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
            completed=[_task("t1", 0.9, duration=12.0, cost=0.15, input_tokens=800)],
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


# ---------------------------------------------------------------------------
# Wilson score confidence interval
# ---------------------------------------------------------------------------


class TestWilsonCI:
    def test_known_values(self) -> None:
        """n=20, passed=15 → bounds approximately (0.531, 0.913)."""
        lo, hi = wilson_ci(15, 20)
        assert lo == pytest.approx(0.531, abs=0.01)
        assert hi == pytest.approx(0.888, abs=0.01)

    def test_all_pass(self) -> None:
        """All passing should have CI upper near 1.0."""
        lo, hi = wilson_ci(10, 10)
        assert lo > 0.6
        assert hi <= 1.0

    def test_none_pass(self) -> None:
        """None passing should have CI lower near 0.0."""
        lo, hi = wilson_ci(0, 10)
        assert lo >= 0.0
        assert hi < 0.4

    def test_zero_total(self) -> None:
        """Zero total returns (0.0, 0.0)."""
        assert wilson_ci(0, 0) == (0.0, 0.0)


# ---------------------------------------------------------------------------
# McNemar's exact test
# ---------------------------------------------------------------------------


class TestMcNemarsExactTest:
    def test_known_contingency(self) -> None:
        """Known discordant pairs produce expected p-value."""
        # 10 paired tasks: a passes all, b fails first 3 and passes rest
        a_scores = [1.0] * 10
        b_scores = [0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
        # Discordant: n10=3 (a pass, b fail), n01=0 (a fail, b pass)
        # n=3, k=min(0,3)=0 → p = 2 * C(3,0)*0.5^3 = 2*0.125 = 0.25
        p = mcnemars_exact_test(a_scores, b_scores)
        assert p == pytest.approx(0.25)

    def test_no_discordant_pairs(self) -> None:
        """Identical outcomes return None."""
        scores = [1.0, 0.0, 1.0]
        assert mcnemars_exact_test(scores, scores) is None

    def test_symmetric_discordance(self) -> None:
        """Equal discordant pairs in both directions → p=1.0."""
        a = [1.0, 0.0, 1.0, 0.0]
        b = [0.0, 1.0, 1.0, 0.0]
        # n10=1, n01=1 → n=2, k=1 → p = 2 * (C(2,0)+C(2,1))*0.25 = 2*0.75 = 1.0 (clamped)
        p = mcnemars_exact_test(a, b)
        assert p == pytest.approx(1.0)

    def test_unequal_lengths(self) -> None:
        """Unequal lengths return None."""
        assert mcnemars_exact_test([1.0, 0.0], [1.0]) is None


# ---------------------------------------------------------------------------
# Wilcoxon signed-rank test
# ---------------------------------------------------------------------------


class TestWilcoxonTest:
    def test_different_scores(self) -> None:
        """Clearly different scores produce a p-value."""
        a = [0.9, 0.8, 0.85, 0.95, 0.7, 0.88, 0.92, 0.87]
        b = [0.1, 0.2, 0.15, 0.05, 0.3, 0.12, 0.08, 0.13]
        p = wilcoxon_test(a, b)
        assert p is not None
        assert p < 0.05

    def test_identical_scores(self) -> None:
        """Identical scores return None (all diffs zero)."""
        a = [0.5, 0.5, 0.5]
        assert wilcoxon_test(a, a) is None

    def test_too_few_samples(self) -> None:
        """Single pair returns None."""
        assert wilcoxon_test([1.0], [0.0]) is None


# ---------------------------------------------------------------------------
# Cliff's delta
# ---------------------------------------------------------------------------


class TestCliffsDelta:
    def test_perfect_dominance(self) -> None:
        """All a > b → delta = 1.0."""
        assert cliffs_delta([1.0, 1.0, 1.0], [0.0, 0.0, 0.0]) == pytest.approx(1.0)

    def test_reverse_dominance(self) -> None:
        """All b > a → delta = -1.0."""
        assert cliffs_delta([0.0, 0.0], [1.0, 1.0]) == pytest.approx(-1.0)

    def test_no_difference(self) -> None:
        """Equal lists → delta = 0.0."""
        assert cliffs_delta([0.5, 0.5], [0.5, 0.5]) == pytest.approx(0.0)

    def test_mixed(self) -> None:
        """Mixed dominance produces expected value."""
        # a=[1,1,1,0] vs b=[0,0,1,0]
        # more: (1>0)=6, (1>0)=6, (1>0)=6, none for 0 → count pairs:
        # a=1 vs b=[0,0,1,0]: 1>0, 1>0, 1=1, 1>0 → 3 more, 0 less
        # × 3 a=1 elements → 9 more, 0 less
        # a=0 vs b=[0,0,1,0]: 0=0, 0=0, 0<1, 0=0 → 0 more, 1 less
        # total: 9 more, 1 less out of 16
        d = cliffs_delta([1.0, 1.0, 1.0, 0.0], [0.0, 0.0, 1.0, 0.0])
        assert d == pytest.approx(0.5)

    def test_empty(self) -> None:
        """Empty input returns 0.0."""
        assert cliffs_delta([], [1.0]) == 0.0
        assert cliffs_delta([1.0], []) == 0.0


# ---------------------------------------------------------------------------
# Cohen's d
# ---------------------------------------------------------------------------


class TestCohensD:
    def test_known_values(self) -> None:
        """Two groups with known means and stds."""
        # a: mean=0.8, b: mean=0.2, both have some variance
        a = [0.7, 0.8, 0.9]
        b = [0.1, 0.2, 0.3]
        d = cohens_d(a, b)
        # mean_diff=0.6, var_a=var_b=0.01, pooled_std=0.1
        assert d == pytest.approx(6.0)

    def test_no_difference(self) -> None:
        """Identical means → d ≈ 0."""
        a = [0.5, 0.5, 0.5]
        b = [0.5, 0.5, 0.5]
        assert cohens_d(a, b) == pytest.approx(0.0)

    def test_zero_variance(self) -> None:
        """Zero variance returns 0.0 to avoid division by zero."""
        a = [1.0, 1.0]
        b = [1.0, 1.0]
        assert cohens_d(a, b) == 0.0

    def test_too_few(self) -> None:
        """Single element returns 0.0."""
        assert cohens_d([1.0], [0.0]) == 0.0


# ---------------------------------------------------------------------------
# ConfigSummary: Wilson CI and sample-size warning
# ---------------------------------------------------------------------------


class TestConfigSummaryStatFields:
    def test_wilson_ci_populated(self) -> None:
        """summarize_config populates ci_lower and ci_upper."""
        results = ConfigResults(
            config="ci-test",
            completed=[
                _task("t1", 1.0),
                _task("t2", 1.0),
                _task("t3", 1.0),
                _task("t4", 0.0),
                _task("t5", 1.0),
            ]
            * 4,  # 20 tasks, 16 passing
        )
        s = summarize_config(results)
        assert 0.0 < s.ci_lower < s.pass_rate
        assert s.pass_rate < s.ci_upper <= 1.0

    def test_sample_size_warning_small(self) -> None:
        """N < 10 triggers warning."""
        results = ConfigResults(
            config="small",
            completed=[_task("t1", 1.0)],
        )
        s = summarize_config(results)
        assert s.sample_size_warning is not None
        assert "Small sample" in s.sample_size_warning

    def test_no_warning_large_sample(self) -> None:
        """N >= 10 has no warning."""
        results = ConfigResults(
            config="large",
            completed=[_task(f"t{i}", 1.0) for i in range(10)],
        )
        s = summarize_config(results)
        assert s.sample_size_warning is None

    def test_billing_model(self) -> None:
        """billing_model reflects dominant cost_model from tasks."""
        results = ConfigResults(
            config="billed",
            completed=[
                CompletedTask(task_id="t1", automated_score=1.0, cost_model="api"),
                CompletedTask(task_id="t2", automated_score=1.0, cost_model="api"),
                CompletedTask(task_id="t3", automated_score=1.0, cost_model="session"),
            ],
        )
        s = summarize_config(results)
        assert s.billing_model == "api"

    def test_billing_model_unknown_default(self) -> None:
        """Tasks with no cost_model set default to 'unknown'."""
        results = ConfigResults(
            config="default",
            completed=[_task("t1", 1.0)],
        )
        s = summarize_config(results)
        assert s.billing_model == "unknown"


# ---------------------------------------------------------------------------
# PairwiseComparison: statistical fields
# ---------------------------------------------------------------------------


class TestCompareConfigsStatistical:
    def _make_summary(self, label: str, **kwargs: object) -> ConfigSummary:
        defaults: dict[str, object] = dict(
            total_tasks=5,
            completed=5,
            errored=0,
            pass_rate=0.8,
            mean_score=0.8,
            median_score=0.8,
            total_duration_sec=50.0,
            mean_duration_sec=10.0,
            total_cost_usd=0.50,
            total_tokens=2000,
        )
        defaults.update(kwargs)
        return ConfigSummary(label=label, **defaults)

    def test_without_scores_defaults_none(self) -> None:
        """Without raw scores, statistical fields are default."""
        a = self._make_summary("a", mean_score=0.9)
        b = self._make_summary("b", mean_score=0.7)
        cmp = compare_configs(a, b)
        assert cmp.p_value is None
        assert cmp.effect_size is None
        assert cmp.effect_size_method == ""

    def test_binary_scores_uses_mcnemar(self) -> None:
        """Binary scores trigger McNemar + Cliff's delta."""
        a = self._make_summary("a", mean_score=0.8)
        b = self._make_summary("b", mean_score=0.4)
        a_scores = [1.0, 1.0, 1.0, 1.0, 0.0]
        b_scores = [0.0, 0.0, 1.0, 1.0, 0.0]
        cmp = compare_configs(a, b, a_scores=a_scores, b_scores=b_scores)
        assert cmp.effect_size_method == "cliffs_delta"
        assert cmp.effect_size is not None

    def test_continuous_scores_uses_wilcoxon(self) -> None:
        """Continuous scores trigger Wilcoxon + Cohen's d."""
        a = self._make_summary("a", mean_score=0.85)
        b = self._make_summary("b", mean_score=0.45)
        a_scores = [0.9, 0.8, 0.85, 0.95, 0.7, 0.88, 0.92, 0.87]
        b_scores = [0.4, 0.5, 0.45, 0.35, 0.6, 0.42, 0.38, 0.43]
        cmp = compare_configs(a, b, a_scores=a_scores, b_scores=b_scores)
        assert cmp.effect_size_method == "cohens_d"
        assert cmp.effect_size is not None
        assert cmp.p_value is not None
        assert cmp.p_value < 0.05

    def test_ci_computed(self) -> None:
        """CI bounds are computed when scores provided."""
        a = self._make_summary("a")
        b = self._make_summary("b")
        a_scores = [0.9, 0.8, 0.85, 0.95, 0.7]
        b_scores = [0.4, 0.5, 0.45, 0.35, 0.6]
        cmp = compare_configs(a, b, a_scores=a_scores, b_scores=b_scores)
        assert cmp.ci_lower < cmp.ci_upper
        assert cmp.ci_lower > 0  # a clearly better
