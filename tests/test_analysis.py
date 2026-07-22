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
    cost_comparable,
    format_csv_report,
    format_html_report,
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
    cost_source: str = "unavailable",
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> CompletedTask:
    return CompletedTask(
        task_id=task_id,
        automated_score=score,
        status=status,
        duration_seconds=duration,
        cost_usd=cost,
        cost_source=cost_source,
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

    def test_errored_runs_excluded_from_mean_and_pass_rate(self) -> None:
        """codeprobe-h3j4: a non-executed status=="error" run is excluded from
        mean_score / pass_rate (not counted as a real 0.0 failure) and is
        surfaced via errored_count / scored_count."""
        results = ConfigResults(
            config="mixed",
            completed=[
                _task("t1", 1.0),
                _task("t2", 0.0, status="error"),
                _task("t3", 1.0),
            ],
        )
        s = summarize_config(results)

        # Mean and pass-rate over the 2 executed runs only — NOT 2/3.
        assert s.mean_score == pytest.approx(1.0)
        assert s.pass_rate == pytest.approx(1.0)
        assert s.errored_count == 1
        assert s.scored_count == 2
        # Structural counts keep all 3 rows.
        assert s.total_tasks == 3


# ---------------------------------------------------------------------------
# compare_configs
# ---------------------------------------------------------------------------


class TestCompareConfigs:
    # Every comparison passes >= 3 paired scores: compare_configs REFUSES the
    # verdict below the _MIN_PAIRED_TASKS floor (codeprobe-f7rl.8), and these
    # tests exercise the winner/tiebreak chain, not the refusal path.

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
        cmp = compare_configs(
            a, b, a_scores=[0.9, 0.95, 0.85], b_scores=[0.4, 0.45, 0.35]
        )

        assert cmp.config_a == "good"
        assert cmp.config_b == "bad"
        assert cmp.comparable is True
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
        cmp = compare_configs(
            a, b, a_scores=[0.85, 0.9, 0.8], b_scores=[0.7, 0.75, 0.65]
        )

        # Score wins: accurate is the winner
        assert cmp.winner == "accurate"
        assert cmp.score_diff == pytest.approx(0.15)
        assert cmp.cost_diff == pytest.approx(0.40)

    def test_same_score_cost_wins(self) -> None:
        """When scores are equal and costs fully covered, lower cost wins."""
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
            cost_coverage=1.0,
        )
        a = ConfigSummary(label="a", total_cost_usd=0.50, **base)
        b = ConfigSummary(label="b", total_cost_usd=0.30, **base)
        tied = [0.8, 0.8, 0.8]
        cmp = compare_configs(a, b, a_scores=tied, b_scores=tied)

        assert cmp.winner == "b"

    def test_same_score_cost_speed_breaks_tie(self) -> None:
        """codeprobe-b9c #9: equal score+cost → faster mean duration wins."""
        base = dict(
            total_tasks=3,
            completed=3,
            errored=0,
            pass_rate=1.0,
            mean_score=0.8,
            median_score=0.8,
            total_cost_usd=0.40,
            total_tokens=1000,
        )
        slow = ConfigSummary(
            label="slow", total_duration_sec=60.0, mean_duration_sec=20.0, **base
        )
        fast = ConfigSummary(
            label="fast", total_duration_sec=30.0, mean_duration_sec=10.0, **base
        )
        # Order-independent: the faster config wins regardless of arg order.
        tied = [0.8, 0.8, 0.8]
        assert compare_configs(slow, fast, a_scores=tied, b_scores=tied).winner == "fast"
        assert compare_configs(fast, slow, a_scores=tied, b_scores=tied).winner == "fast"

    def test_total_tie_defaults_to_first(self) -> None:
        """codeprobe-b9c #9: a full tie (score+cost+speed) falls back to the
        first config — the deterministic last-resort tiebreaker."""
        base = dict(
            total_tasks=3,
            completed=3,
            errored=0,
            pass_rate=1.0,
            mean_score=0.8,
            median_score=0.8,
            total_duration_sec=30.0,
            mean_duration_sec=10.0,
            total_cost_usd=0.40,
            total_tokens=1000,
        )
        a = ConfigSummary(label="a", **base)
        b = ConfigSummary(label="b", **base)
        tied = [0.8, 0.8, 0.8]
        assert compare_configs(a, b, a_scores=tied, b_scores=tied).winner == "a"
        assert compare_configs(b, a, a_scores=tied, b_scores=tied).winner == "b"


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
            cost_coverage=1.0,
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
            cost_coverage=1.0,
        )
        ranked = rank_configs([best, cheap])

        assert ranked[0].label == "best"
        assert ranked[1].label == "cheap"
        assert "cost-efficiency" in ranked[1].recommendation.lower()

    def test_all_errored_config_trails_and_is_never_best(self) -> None:
        """codeprobe-h3j4: a config whose every run was non-executed
        (scored_count == 0) must NOT win the ranking on a vacuous 0.0 mean —
        it trails the scorable config with an ERRORED recommendation."""
        scorable = ConfigSummary(
            label="real",
            total_tasks=2,
            completed=2,
            errored=0,
            pass_rate=0.5,
            mean_score=0.5,
            median_score=0.5,
            total_duration_sec=20.0,
            mean_duration_sec=10.0,
            total_cost_usd=0.20,
            total_tokens=1000,
        )
        all_errored = ConfigSummary(
            label="broken",
            total_tasks=2,
            completed=0,
            errored=2,
            pass_rate=0.0,
            mean_score=0.0,
            median_score=0.0,
            total_duration_sec=0.0,
            mean_duration_sec=0.0,
            total_cost_usd=None,
            total_tokens=None,
            errored_count=2,
        )
        ranked = rank_configs([all_errored, scorable])

        assert ranked[0].label == "real"
        assert "Best" in ranked[0].recommendation
        assert ranked[1].label == "broken"
        assert "ERRORED" in ranked[1].recommendation

    def test_all_configs_errored_have_no_best(self) -> None:
        """codeprobe-h3j4: when no config produced a scorable run, every entry
        is ERRORED — there is no 'best' to pick."""
        a = ConfigSummary(
            label="a",
            total_tasks=1,
            completed=0,
            errored=1,
            pass_rate=0.0,
            mean_score=0.0,
            median_score=0.0,
            total_duration_sec=0.0,
            mean_duration_sec=0.0,
            total_cost_usd=None,
            total_tokens=None,
            errored_count=1,
        )
        b = ConfigSummary(
            label="b",
            total_tasks=1,
            completed=0,
            errored=1,
            pass_rate=0.0,
            mean_score=0.0,
            median_score=0.0,
            total_duration_sec=0.0,
            mean_duration_sec=0.0,
            total_cost_usd=None,
            total_tokens=None,
            errored_count=1,
        )
        ranked = rank_configs([a, b])

        assert len(ranked) == 2
        assert all("ERRORED" in rc.recommendation for rc in ranked)
        assert all(rc.summary.scored_count == 0 for rc in ranked)


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
                _task("t3", 0.9, duration=12.0, cost=0.11, input_tokens=550),
            ],
        )
        results_b = ConfigResults(
            config="config-b",
            completed=[
                _task("t1", 0.5, duration=8.0, cost=0.05, input_tokens=300),
                _task("t2", 0.3, duration=12.0, cost=0.09, input_tokens=400),
                _task("t3", 0.2, duration=10.0, cost=0.07, input_tokens=350),
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


class TestErroredConfigReporting:
    """codeprobe-h3j4: non-executed runs (status=="error") must not be
    rendered as 0.00 failures, and an all-errored experiment must refuse a
    'Use X' recommendation in favour of a prescriptive re-run message."""

    def test_all_errored_refuses_recommendation(self) -> None:
        results_a = ConfigResults(
            config="config-a",
            completed=[
                _task("t1", 0.0, status="error"),
                _task("t2", 0.0, status="error"),
            ],
        )
        results_b = ConfigResults(
            config="config-b",
            completed=[_task("t1", 0.0, status="error")],
        )
        report = generate_report("all-errored", [results_a, results_b])
        text = format_text_report(report)

        # No confident pick — the recommendation refuses and prescribes a re-run.
        assert "for best results" not in text
        assert "No comparison available" in text
        # Errored configs surface as ERRORED, not as 0% pass-rate rows.
        assert "ERRORED" in text

    def test_mixed_still_recommends_scorable_config(self) -> None:
        # config-a has real scorable runs; config-b is all-errored. The
        # recommendation must pick config-a and never config-b.
        results_a = ConfigResults(
            config="config-a",
            completed=[
                _task("t1", 1.0, cost=0.10),
                _task("t2", 0.8, cost=0.10),
            ],
        )
        results_b = ConfigResults(
            config="config-b",
            completed=[_task("t1", 0.0, status="error")],
        )
        report = generate_report("mixed", [results_a, results_b])
        text = format_text_report(report)

        assert "Use config-a for best results." in text
        assert "Use config-b" not in text


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
# Partial results: per-arm N + worst-arm semantics (codeprobe-f7rl.9)
# ---------------------------------------------------------------------------


class TestPerArmPartial:
    """One complete arm must never mask a crashed one (codeprobe-f7rl.9)."""

    def _two_arm_report(self) -> Report:
        arm_a = ConfigResults(
            config="arm-A",
            completed=[_task(f"t{i}", 1.0) for i in range(10)],
        )
        arm_b = ConfigResults(
            config="arm-B",
            completed=[_task(f"t{i}", 1.0) for i in range(2)],
        )
        return generate_report("uneven", [arm_a, arm_b], total_tasks=10)

    def test_complete_arm_does_not_mask_partial_arm(self) -> None:
        """Regression: max() across arms hid the 2/10 arm behind the 10/10 one."""
        report = self._two_arm_report()
        assert report.is_partial is True
        assert report.completion_ratio == pytest.approx(0.2)

    def test_text_report_names_worst_arm(self) -> None:
        text = format_text_report(self._two_arm_report())
        assert "PARTIAL" in text
        assert "worst arm arm-B" in text
        assert "2/10 tasks (20%)" in text

    def test_text_rankings_show_per_arm_n(self) -> None:
        text = format_text_report(self._two_arm_report())
        assert "N=10/10" in text
        assert "N=2/10" in text

    def test_text_partial_suffix_only_on_incomplete_arm(self) -> None:
        text = format_text_report(self._two_arm_report())
        ranking_lines = [
            line for line in text.splitlines() if line.startswith(("1.", "2."))
        ]
        a_line = next(line for line in ranking_lines if "arm-A" in line)
        b_line = next(line for line in ranking_lines if "arm-B" in line)
        assert "PARTIAL" not in a_line
        assert "⚠ PARTIAL (2/10 tasks)" in b_line

    def test_html_n_column_and_partial_badge(self) -> None:
        html = format_html_report(self._two_arm_report())
        assert "<th>N</th>" in html
        assert "<td>10/10</td>" in html
        assert "<td>2/10</td>" in html
        assert "PARTIAL (2/10 tasks)" in html
        # The complete arm carries no partial badge.
        assert "PARTIAL (10/10 tasks)" not in html

    def test_html_banner_mirrors_worst_arm_wording(self) -> None:
        html = format_html_report(self._two_arm_report())
        assert "worst arm arm-B: 2/10 tasks (20%)" in html

    def test_ranking_still_lists_partial_arm(self) -> None:
        """Partial is disclosed, not hidden — the arm stays in the ranking."""
        report = self._two_arm_report()
        assert {rc.label for rc in report.rankings} == {"arm-A", "arm-B"}

    def test_no_expectation_renders_no_n_suffix(self) -> None:
        """Without total_tasks the rankings carry no N=x/y coverage suffix.

        The small-sample caution ("Small sample size (N=1)",
        codeprobe-f7rl.31) is independent of coverage and still renders.
        """
        arm = ConfigResults(config="solo", completed=[_task("t1", 1.0)])
        report = generate_report("plain", [arm])
        text = format_text_report(report)
        html = format_html_report(report)
        assert "N=1/" not in text  # no distinct/expected coverage suffix
        assert "PARTIAL" not in text
        assert "<td>—</td>" in html  # N column shows an em dash


def test_repeats_do_not_mask_partial() -> None:
    """6 trials over 2 of 4 expected tasks is partial (codeprobe-f7rl.9).

    The old trial-count check saw 6 > 4 and reported complete.
    """
    trials = [_task(f"t{i}", 1.0) for i in range(2) for _ in range(3)]
    results = ConfigResults(config="rep", completed=trials)
    s = summarize_config(results, total_tasks=4)
    assert s.distinct_task_count == 2
    assert s.is_partial is True

    report = generate_report("rep-exp", [results], total_tasks=4)
    assert report.is_partial is True
    assert report.completion_ratio == pytest.approx(0.5)


def test_repeats_do_not_fake_partiality() -> None:
    """All expected tasks covered by repeats → NOT partial."""
    trials = [_task(f"t{i}", 1.0) for i in range(2) for _ in range(3)]
    s = summarize_config(ConfigResults(config="rep", completed=trials), total_tasks=2)
    assert s.distinct_task_count == 2
    assert s.is_partial is False


def test_streaming_batch_partial_parity() -> None:
    """Streaming and batch reports agree on all partial metadata."""
    a_tasks = [_task(f"t{i}", 1.0) for i in range(10)]
    b_tasks = [_task(f"t{i}", 1.0) for i in range(2)]
    batch = generate_report(
        "parity",
        [
            ConfigResults(config="A", completed=a_tasks),
            ConfigResults(config="B", completed=b_tasks),
        ],
        total_tasks=10,
    )

    def stream() -> Iterator[tuple[str, Iterator[CompletedTask]]]:
        yield ("A", iter(a_tasks))
        yield ("B", iter(b_tasks))

    streamed = generate_report_streaming("parity", stream(), total_tasks=10)

    assert batch.is_partial is True
    assert streamed.is_partial is True
    assert streamed.completion_ratio == pytest.approx(batch.completion_ratio)
    assert [s.distinct_task_count for s in streamed.summaries] == [
        s.distinct_task_count for s in batch.summaries
    ]
    assert [s.is_partial for s in streamed.summaries] == [
        s.is_partial for s in batch.summaries
    ]


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


# ---------------------------------------------------------------------------
# Refused (NOT COMPARABLE) pairs across report surfaces (codeprobe-f7rl.8)
# ---------------------------------------------------------------------------


class TestRefusedComparisonSurfaces:
    """Disjoint arms are refused on every surface: text, HTML, and JSON."""

    def _disjoint_report(self) -> Report:
        results_a = ConfigResults(
            config="arm-a",
            completed=[
                _task("a1", 1.0, duration=10.0, cost=0.10),
                _task("a2", 0.9, duration=11.0, cost=0.12),
                _task("a3", 0.8, duration=12.0, cost=0.11),
            ],
        )
        results_b = ConfigResults(
            config="arm-b",
            completed=[
                _task("b1", 0.5, duration=20.0, cost=0.05),
                _task("b2", 0.4, duration=21.0, cost=0.06),
                _task("b3", 0.3, duration=22.0, cost=0.07),
            ],
        )
        return generate_report("disjoint-exp", [results_a, results_b])

    def test_disjoint_arms_comparison_refused(self) -> None:
        report = self._disjoint_report()
        assert len(report.comparisons) == 1
        c = report.comparisons[0]
        assert c.comparable is False
        assert c.winner == ""
        assert "disjoint task sets" in c.refusal_reason

    def test_text_report_contains_refusal(self) -> None:
        text = format_text_report(self._disjoint_report())
        assert "NOT COMPARABLE" in text

    def test_html_report_refuses_winner_badge(self) -> None:
        html = format_html_report(self._disjoint_report())
        assert "NOT COMPARABLE" in html
        assert "Winner:" not in html
        assert "disjoint task sets" in html

    def test_json_report_carries_refusal_fields(self) -> None:
        data = json.loads(format_json_report(self._disjoint_report()))
        (c,) = data["comparisons"]
        assert c["comparable"] is False
        assert "disjoint task sets" in c["refusal_reason"]
        assert c["winner"] == ""

    def test_html_renders_softened_verdict(self) -> None:
        """Comparable but not-significant pair: the softened verdict sentence
        reaches the HTML card, not just the text report (same numbers as
        tests/test_stats.py test_small_effect_softens_verdict)."""
        a_scores = [0.95, 0.10, 0.85, 0.20, 0.75, 0.30]
        b_scores = [0.93, 0.08, 0.83, 0.18, 0.72, 0.28]
        results_a = ConfigResults(
            config="arm-a",
            completed=[
                _task(f"t{i}", s, duration=10.0) for i, s in enumerate(a_scores)
            ],
        )
        results_b = ConfigResults(
            config="arm-b",
            completed=[
                _task(f"t{i}", s, duration=10.0) for i, s in enumerate(b_scores)
            ],
        )
        report = generate_report("soft-exp", [results_a, results_b])
        c = report.comparisons[0]
        assert c.comparable is True
        assert "nominally ahead" in c.summary

        html = format_html_report(report)
        assert "nominally ahead" in html


# ---------------------------------------------------------------------------
# format_csv_report
# ---------------------------------------------------------------------------


def _csv_strip_comments(text: str) -> str:
    """Remove comment lines starting with '#' for csv.DictReader."""
    return "\n".join(line for line in text.splitlines() if not line.startswith("#"))


class TestFormatCsvReport:
    def test_csv_columns(self) -> None:
        """CSV output has the required columns."""
        results = ConfigResults(
            config="alpha",
            completed=[
                _task(
                    f"t{i}",
                    1.0,
                    duration=10.0,
                    cost=0.20,
                    input_tokens=500,
                    output_tokens=100,
                )
                for i in range(10)
            ],
        )
        report = generate_report("csv-exp", [results])
        text = format_csv_report(report)

        reader = csv.DictReader(io.StringIO(_csv_strip_comments(text)))
        expected_cols = {
            "config",
            "task_id",
            "repeat",
            "score",
            "pass",
            "duration_sec",
            "cost_usd",
            "cost_source",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_creation_tokens",
            "cost_model",
            "ci_lower",
            "ci_upper",
            # Dual scoring leg columns (always present in schema; blank
            # for single-scored tasks).
            "score_direct",
            "score_artifact",
            "passed_direct",
            "passed_artifact",
            "scoring_policy",
            # Tool-benefit delta columns (always present in schema; blank
            # for tasks without a mine-time expected_tool_benefit).
            "expected_tool_benefit",
            "tool_call_count",
            "tool_delta_vs_expected",
            # R17: per-checkpoint partial-credit map (JSON-encoded in the
            # CSV cell; blank for non-checkpoint tasks).
            "checkpoint_scores",
        }
        assert reader.fieldnames is not None
        assert set(reader.fieldnames) == expected_cols

    def test_csv_data_rows(self) -> None:
        """CSV has correct data per task."""
        results = ConfigResults(
            config="beta",
            completed=[
                CompletedTask(
                    task_id="t1",
                    automated_score=1.0,
                    duration_seconds=10.0,
                    cost_usd=0.15,
                    cost_source="api",
                    input_tokens=500,
                    output_tokens=100,
                    cache_read_tokens=50,
                    cost_model="gpt-4o",
                ),
                CompletedTask(
                    task_id="t2",
                    automated_score=0.0,
                    duration_seconds=20.0,
                ),
            ],
        )
        report = generate_report("csv-exp", [results])
        text = format_csv_report(report)

        rows = list(csv.DictReader(io.StringIO(_csv_strip_comments(text))))
        assert len(rows) == 2

        assert rows[0]["config"] == "beta"
        assert rows[0]["task_id"] == "t1"
        assert rows[0]["repeat"] == "1"
        assert rows[0]["score"] == "1.0"
        assert rows[0]["pass"] == "1"
        assert rows[0]["cost_usd"] == "0.15"
        assert rows[0]["cost_source"] == "api"
        assert rows[0]["input_tokens"] == "500"
        assert rows[0]["cache_read_tokens"] == "50"
        assert rows[0]["cost_model"] == "gpt-4o"

        assert rows[1]["pass"] == "0"
        assert rows[1]["cost_usd"] == ""

    def test_csv_warning_comment_small_sample(self) -> None:
        """CSV includes an accurate small-sample comment, not 'SINGLE RUN'."""
        results = ConfigResults(
            config="tiny",
            completed=[_task("t1", 1.0, duration=5.0)],
        )
        report = generate_report("warn-exp", [results])
        text = format_csv_report(report)

        assert text.startswith("# SMALL SAMPLE")
        assert "interpret confidence intervals with caution" in text
        assert "SINGLE RUN" not in text

    def test_csv_no_warning_large_sample(self) -> None:
        """CSV has no warning when sample is large enough."""
        results = ConfigResults(
            config="big",
            completed=[_task(f"t{i}", 1.0, duration=5.0) for i in range(10)],
        )
        report = generate_report("ok-exp", [results])
        text = format_csv_report(report)

        assert not text.startswith("#")

    def test_csv_multiple_configs(self) -> None:
        """CSV includes rows from multiple configs."""
        results_a = ConfigResults(
            config="a",
            completed=[_task("t1", 1.0, duration=10.0)],
        )
        results_b = ConfigResults(
            config="b",
            completed=[_task("t1", 0.5, duration=20.0)],
        )
        report = generate_report("multi", [results_a, results_b])
        text = format_csv_report(report)
        rows = list(csv.DictReader(io.StringIO(_csv_strip_comments(text))))

        configs = {r["config"] for r in rows}
        assert configs == {"a", "b"}


# ---------------------------------------------------------------------------
# format_text_report: per-task table
# ---------------------------------------------------------------------------


class TestFormatTextReportPerTask:
    def test_per_task_table_present(self) -> None:
        """Text report includes per-task table with task data."""
        results = ConfigResults(
            config="alpha",
            completed=[
                _task("t1", 1.0, duration=10.0, cost=0.20),
                _task("t2", 0.0, duration=15.0, cost=0.22),
            ],
        )
        report = generate_report("test-exp", [results])
        text = format_text_report(report)

        assert "### Per-Task Results" in text
        assert "t1" in text
        assert "t2" in text
        assert "| alpha |" in text


# ---------------------------------------------------------------------------
# format_html_report
# ---------------------------------------------------------------------------


class TestFormatHtmlReport:
    def test_self_contained_html(self) -> None:
        """HTML output is self-contained with inline CSS/JS."""
        results = ConfigResults(
            config="alpha",
            completed=[
                _task("t1", 1.0, duration=10.0, cost=0.20),
                _task("t2", 0.8, duration=15.0, cost=0.22),
            ],
        )
        report = generate_report("html-exp", [results])
        html = format_html_report(report)

        assert html.startswith("<!DOCTYPE html>")
        assert "<style>" in html
        assert "<script>" in html
        assert "</html>" in html
        # No external links
        assert 'href="http' not in html
        assert 'src="http' not in html

    def test_executive_summary_section(self) -> None:
        """HTML contains executive summary with recommendation."""
        results = ConfigResults(
            config="best-agent",
            completed=[_task("t1", 1.0, duration=10.0, cost=0.10)],
        )
        report = generate_report("exec-exp", [results])
        html = format_html_report(report)

        assert 'id="executive-summary"' in html
        assert "best-agent" in html
        assert "Recommendation" in html

    def test_ranking_table(self) -> None:
        """HTML contains ranking table with scores and costs."""
        results_a = ConfigResults(
            config="fast",
            completed=[_task("t1", 1.0, duration=5.0, cost=0.50)],
        )
        results_b = ConfigResults(
            config="slow",
            completed=[_task("t1", 0.5, duration=20.0, cost=0.10)],
        )
        report = generate_report("rank-exp", [results_a, results_b])
        html = format_html_report(report)

        assert 'id="ranking-table"' in html
        assert "fast" in html
        assert "slow" in html
        assert "Pass Rate" in html
        assert "Mean Score" in html

    def test_per_task_drilldown(self) -> None:
        """HTML contains per-task drill-down with details elements."""
        results = ConfigResults(
            config="drill",
            completed=[
                _task("task-a", 1.0, duration=10.0, cost=0.20),
                _task("task-b", 0.0, duration=15.0, cost=0.22),
            ],
        )
        report = generate_report("drill-exp", [results])
        html = format_html_report(report)

        assert 'id="per-task-drilldown"' in html
        assert "<details>" in html
        assert "task-a" in html
        assert "task-b" in html

    def test_pairwise_comparison_cards(self) -> None:
        """HTML contains pairwise comparison cards (6 clearly separated
        shared tasks so the win is significant and gets a Winner badge —
        a softened verdict renders a warning badge instead,
        codeprobe-f7rl.31)."""
        a_scores = [1.0, 0.9, 0.95, 0.85, 0.8, 0.75]
        b_scores = [0.4, 0.35, 0.3, 0.25, 0.2, 0.15]
        results_a = ConfigResults(
            config="alpha",
            completed=[
                _task(f"t{i}", s, duration=10.0, cost=0.20)
                for i, s in enumerate(a_scores)
            ],
        )
        results_b = ConfigResults(
            config="beta",
            completed=[
                _task(f"t{i}", s, duration=20.0, cost=0.10)
                for i, s in enumerate(b_scores)
            ],
        )
        report = generate_report("pair-exp", [results_a, results_b])
        html = format_html_report(report)

        assert 'id="pairwise-comparisons"' in html
        assert "pairwise-card" in html
        assert "Score diff" in html
        assert "Winner" in html

    def test_cost_efficiency_section(self) -> None:
        """HTML contains cost efficiency section with billing model separation."""
        results = ConfigResults(
            config="api-agent",
            completed=[
                CompletedTask(
                    task_id="t1",
                    automated_score=1.0,
                    duration_seconds=10.0,
                    cost_usd=0.20,
                    cost_model="api",
                ),
            ],
        )
        report = generate_report("cost-exp", [results])
        html = format_html_report(report)

        assert 'id="cost-efficiency"' in html
        assert "Per-Token Billing" in html
        assert "Subscription Billing" in html

    def test_small_sample_banner_keeps_ci(self) -> None:
        """Small samples get the accurate stats-layer warning — never the
        false 'Single run' wording — and CI bars stay rendered
        (codeprobe-f7rl.31)."""
        results = ConfigResults(
            config="single",
            completed=[_task("t1", 1.0, duration=5.0)],
        )
        report = generate_report("single-exp", [results])
        html = format_html_report(report)

        assert "Single run" not in html
        assert "Small sample size (N=1)" in html
        assert "small-sample-banner" in html
        assert "small-sample-badge" in html
        assert "ci-bar" in html

    def test_no_small_sample_banner_large_sample(self) -> None:
        """Large sample does not show the small-sample banner or badge."""
        results = ConfigResults(
            config="large",
            completed=[_task(f"t{i}", 1.0, duration=5.0) for i in range(10)],
        )
        report = generate_report("large-exp", [results])
        html = format_html_report(report)

        # CSS classes are always in <style>; assert no rendered elements.
        assert '<div class="small-sample-banner">' not in html
        assert '<span class="small-sample-badge">' not in html
        assert "Small sample size" not in html

    def test_partial_report_banner(self) -> None:
        """Partial report shows completion info in HTML."""
        results = ConfigResults(
            config="partial",
            completed=[_task("t1", 1.0, duration=5.0)],
        )
        report = generate_report("partial-exp", [results], total_tasks=10)
        html = format_html_report(report)

        assert "PARTIAL" in html
        assert "1/10" in html


# ---------------------------------------------------------------------------
# format_html_report: reward-population exclusions + validity gate
# (codeprobe-gu9m — the HTML surface must tell the same story as text/JSON)
# ---------------------------------------------------------------------------


def _token_ceiling_crash(task_id: str = "crash") -> CompletedTask:
    """Infra casualty: output-token ceiling overrun, no scoring (codeprobe-77z)."""
    return CompletedTask(
        task_id=task_id,
        automated_score=0.0,
        status="error",
        error_category="agent",
        metadata={"error": "API Error: exceeded the 32000 output token maximum"},
    )


class TestHtmlExclusionsAndValidity:
    def _crashed_report(self) -> Report:
        results = ConfigResults(
            config="arm-A",
            completed=[
                _task("t1", 1.0),
                _task("t2", 0.0),
                _token_ceiling_crash("t3"),
            ],
        )
        return generate_report("infra-exp", [results])

    def test_infra_exclusion_count_and_fail_verdict(self) -> None:
        """An infra casualty shows its exclusion count AND the FAIL verdict."""
        html = format_html_report(self._crashed_report())

        # Exclusion badge, worded exactly as the text report's suffix.
        assert "1 infra failure(s)" in html
        # Run-level validity verdict.
        assert 'id="validity"' in html
        assert "VALIDITY FAIL" in html
        assert "t3#rep0" in html
        assert "NOT quotable" in html

    def test_text_and_html_agree_on_exclusions(self) -> None:
        """Same run, same wording on both surfaces."""
        report = self._crashed_report()
        text = format_text_report(report)
        html = format_html_report(report)
        for phrase in ("1 infra failure(s)", "VALIDITY FAIL", "NOT quotable"):
            assert phrase in text
            assert phrase in html

    def test_quota_and_errored_badges(self) -> None:
        """Quota, non-quota infra, and other-errored counts are each shown."""
        results = ConfigResults(
            config="arm",
            completed=[
                _task("t1", 1.0),
                CompletedTask(
                    task_id="t2",
                    automated_score=0.0,
                    status="error",
                    error_category="quota",
                    metadata={"error": "OAuth token usage limit reached"},
                ),
                _token_ceiling_crash("t3"),
                # Excluded from scoring (status == "error") but NOT an infra
                # casualty: the adapter declared a terminal subtype, so the gate
                # does not ask for a re-run.
                CompletedTask(
                    task_id="t4",
                    automated_score=0.0,
                    status="error",
                    error_category="agent",
                    result_subtype="error_max_turns",
                    metadata={"error": "agent stopped after reaching the max turns"},
                ),
            ],
        )
        html = format_html_report(generate_report("mixed-exp", [results]))

        assert "1 quota error(s)" in html
        assert "1 infra failure(s)" in html
        assert "1 errored (excluded)" in html

    def test_unscorable_config_shows_errored_not_zero_mean(self) -> None:
        """A config where nothing ran renders ERRORED, not a vacuous 0.00 mean."""
        results = ConfigResults(
            config="dead-arm",
            completed=[_task("t1", 0.0, status="error")],
        )
        html = format_html_report(generate_report("dead-exp", [results]))

        assert "ERRORED (1)" in html

    def test_clean_run_passes_gate_with_no_fail_banner(self) -> None:
        """A clean run says PASS and never claims the run is unquotable."""
        results = ConfigResults(config="arm", completed=[_task("t1", 1.0)])
        html = format_html_report(generate_report("clean-exp", [results]))

        assert "VALIDITY PASS" in html
        assert "NOT quotable" not in html

    def test_validity_summary_is_html_escaped(self) -> None:
        """Trial ids reach the page through the escaper, never raw."""
        results = ConfigResults(
            config="arm",
            completed=[_task("t1", 1.0), _token_ceiling_crash("<script>x</script>")],
        )
        html = format_html_report(generate_report("esc-exp", [results]))

        assert "<script>x</script>" not in html
        assert "&lt;script&gt;x&lt;/script&gt;" in html


# ---------------------------------------------------------------------------
# format_json_report: per-task data
# ---------------------------------------------------------------------------


class TestFormatJsonReportPerTask:
    def test_tasks_array_present(self) -> None:
        """JSON report includes tasks array with per-task data."""
        results = ConfigResults(
            config="gamma",
            completed=[
                CompletedTask(
                    task_id="t1",
                    automated_score=1.0,
                    duration_seconds=10.0,
                    cost_usd=0.15,
                    cost_source="api",
                    input_tokens=500,
                    output_tokens=100,
                    cache_read_tokens=50,
                    cost_model="gpt-4o",
                ),
            ],
        )
        report = generate_report("json-exp", [results])
        data = json.loads(format_json_report(report))

        assert "tasks" in data
        assert len(data["tasks"]) == 1

        task = data["tasks"][0]
        assert task["config"] == "gamma"
        assert task["task_id"] == "t1"
        assert task["repeat"] == 1
        assert task["score"] == 1.0
        assert task["pass"] == 1
        assert task["duration_sec"] == 10.0
        assert task["cost_usd"] == 0.15
        assert task["cost_source"] == "api"
        assert task["input_tokens"] == 500
        assert task["output_tokens"] == 100
        assert task["cache_read_tokens"] == 50
        assert task["cost_model"] == "gpt-4o"
        assert "ci_lower" in task
        assert "ci_upper" in task

    def test_tasks_empty_for_streaming(self) -> None:
        """Streaming report has empty tasks array (no config_results)."""
        tasks = [_task("t1", 0.9, duration=5.0)]

        def stream() -> Iterator[tuple[str, Iterator[CompletedTask]]]:
            yield ("solo", iter(tasks))

        report = generate_report_streaming("solo-exp", stream())
        data = json.loads(format_json_report(report))
        assert data["tasks"] == []


# ---------------------------------------------------------------------------
# Repeats: per-task means as the statistical unit (codeprobe-f7rl.7)
# ---------------------------------------------------------------------------


class TestRepeatsPerTaskMean:
    """Repeat trials must not overwrite each other in pairwise stats.

    Fixture: 2 configs x 3 tasks x 3 repeats where the LAST repeat per
    task is equal across arms (0.5), so code that keys trials by task_id
    alone collapses to a 0.5-vs-0.5 tie (p=None / effect=0.0). Per-task
    means differ strongly (A ~0.8 vs B ~0.2). The tasks' means differ
    slightly so the paired diffs have nonzero variance — cohens_d
    returns 0.0 for zero pooled variance, so an identical-means fixture
    cannot distinguish the fix (drift from the bead's exact fixture,
    same collapse property). Three tasks, not two, so the pair clears
    the _MIN_PAIRED_TASKS refusal floor (codeprobe-f7rl.8).
    """

    # Per-task repeat scores, in repeat order (repeat_index 0, 1, 2).
    _A = {"t1": [1.0, 1.0, 0.5], "t2": [1.0, 0.9, 0.5], "t3": [1.0, 0.8, 0.5]}
    _B = {"t1": [0.0, 0.0, 0.5], "t2": [0.0, 0.1, 0.5], "t3": [0.0, 0.2, 0.5]}

    @staticmethod
    def _repeat_tasks(scores: dict[str, list[float]]) -> list[CompletedTask]:
        return [
            CompletedTask(
                task_id=tid,
                automated_score=score,
                repeat_index=idx,
                duration_seconds=10.0,
            )
            for tid, repeats in scores.items()
            for idx, score in enumerate(repeats)
        ]

    def _results(self) -> list[ConfigResults]:
        return [
            ConfigResults(config="arm-a", completed=self._repeat_tasks(self._A)),
            ConfigResults(config="arm-b", completed=self._repeat_tasks(self._B)),
        ]

    def test_comparison_uses_all_repeats(self) -> None:
        report = generate_report("repeats-exp", self._results())
        assert len(report.comparisons) == 1
        comp = report.comparisons[0]

        # Old code collapsed both arms to the last repeat (0.5 vs 0.5):
        # p_value=None and effect_size=0.0. Per-task means (A ~0.82 vs
        # B ~0.18) give a real effect.
        assert comp.effect_size is not None
        assert comp.effect_size != 0.0
        assert comp.p_value is not None

    def test_direction_agrees_with_summary_means(self) -> None:
        report = generate_report("repeats-exp", self._results())
        comp = report.comparisons[0]
        by_label = {s.label: s for s in report.summaries}

        # Summary means run over all trials: arm-a is clearly ahead.
        assert by_label["arm-a"].mean_score > by_label["arm-b"].mean_score
        # The comparison must point the same way (a minus b positive).
        assert comp.score_diff > 0
        assert comp.effect_size is not None and comp.effect_size > 0

    def test_streaming_matches_batch(self) -> None:
        batch = generate_report("repeats-exp", self._results())

        def stream() -> Iterator[tuple[str, Iterator[CompletedTask]]]:
            yield ("arm-a", iter(self._repeat_tasks(self._A)))
            yield ("arm-b", iter(self._repeat_tasks(self._B)))

        streaming = generate_report_streaming("repeats-exp", stream())

        assert len(streaming.comparisons) == 1
        b_comp, s_comp = batch.comparisons[0], streaming.comparisons[0]
        assert s_comp.p_value == b_comp.p_value
        assert s_comp.effect_size == b_comp.effect_size
        assert s_comp.score_diff == pytest.approx(b_comp.score_diff)

    def test_json_rows_carry_real_repeat_numbers(self) -> None:
        report = generate_report("repeats-exp", self._results())
        data = json.loads(format_json_report(report))

        t1_repeats = {
            row["repeat"]
            for row in data["tasks"]
            if row["config"] == "arm-a" and row["task_id"] == "t1"
        }
        assert t1_repeats == {1, 2, 3}

    def test_csv_rows_carry_real_repeat_numbers(self) -> None:
        report = generate_report("repeats-exp", self._results())
        text = format_csv_report(report)

        rows = list(csv.DictReader(io.StringIO(_csv_strip_comments(text))))
        t1_repeats = {
            row["repeat"]
            for row in rows
            if row["config"] == "arm-a" and row["task_id"] == "t1"
        }
        assert t1_repeats == {"1", "2", "3"}


class TestKArmCorrection:
    """k>2 experiments Holm-correct the pairwise family (codeprobe-f7rl.10).

    Fixture: 3 binary arms over 6 shared tasks. arm-a vs arm-b has 6
    discordant pairs -> McNemar exact raw p = 2/64 = 0.03125, inside
    (0.05/2, 0.05): significant uncorrected, NOT significant after Holm
    (adjusted = 3 * 0.03125 = 0.09375). arm-c alternates, so both pairs
    against it have 3 discordant pairs -> raw p = 0.25.
    """

    _TASKS = [f"t{i}" for i in range(6)]
    _A = [1.0] * 6
    _B = [0.0] * 6
    _C = [1.0, 0.0, 1.0, 0.0, 1.0, 0.0]

    def _arm(self, label: str, scores: list[float]) -> ConfigResults:
        return ConfigResults(
            config=label,
            completed=[_task(tid, s) for tid, s in zip(self._TASKS, scores)],
        )

    def _three_arm_report(self) -> Report:
        return generate_report(
            "karm-exp",
            [
                self._arm("arm-a", self._A),
                self._arm("arm-b", self._B),
                self._arm("arm-c", self._C),
            ],
        )

    def _two_arm_report(self) -> Report:
        return generate_report(
            "karm-exp", [self._arm("arm-a", self._A), self._arm("arm-b", self._B)]
        )

    def test_wins_gated_on_adjusted_p(self) -> None:
        """The (0.025, 0.05) raw pair loses 'wins' after Holm."""
        report = self._three_arm_report()
        ab = report.comparisons[0]
        assert ab.config_a == "arm-a" and ab.config_b == "arm-b"
        assert ab.p_value == pytest.approx(0.03125)
        assert ab.p_value_adjusted == pytest.approx(0.09375)
        assert ab.p_value_adjusted > 0.05
        assert ab.correction == "holm"
        assert ab.n_comparisons == 3
        assert "wins" not in ab.summary
        assert "not significant" in ab.summary

    def test_all_pairs_carry_family_metadata(self) -> None:
        report = self._three_arm_report()
        assert len(report.comparisons) == 3
        for c in report.comparisons:
            assert c.correction == "holm"
            assert c.n_comparisons == 3
            assert c.p_value_adjusted is not None
            assert c.p_value_adjusted >= c.p_value

    def test_two_arm_report_unchanged(self) -> None:
        """k=2 is a single test: no correction, raw verdict stands."""
        report = self._two_arm_report()
        assert len(report.comparisons) == 1
        c = report.comparisons[0]
        assert c.correction == "none"
        assert c.n_comparisons == 1
        assert c.p_value_adjusted == c.p_value == pytest.approx(0.03125)
        assert "wins" in c.summary

    def test_text_disclosure_only_for_k_gt_2(self) -> None:
        disclosure = (
            "3 arms -> 3 pairwise tests; p-values Holm-corrected "
            "(family-wise alpha=0.05)"
        )
        assert disclosure in format_text_report(self._three_arm_report())
        assert "Holm" not in format_text_report(self._two_arm_report())

    def test_html_disclosure_only_for_k_gt_2(self) -> None:
        html3 = format_html_report(self._three_arm_report())
        assert "3 arms -&gt; 3 pairwise tests" in html3 or (
            "3 arms -> 3 pairwise tests" in html3
        )
        assert "Holm-corrected (family-wise alpha=0.05)" in html3
        assert "p-value (Holm-adj.)" in html3
        assert "0.0938" in html3  # adjusted value rendered
        assert "raw 0.0312" in html3  # raw value kept in parentheses
        assert "Holm" not in format_html_report(self._two_arm_report())

    def test_json_exposes_correction_fields(self) -> None:
        data = json.loads(format_json_report(self._three_arm_report()))
        ab = data["comparisons"][0]
        assert ab["p_value"] == pytest.approx(0.03125)
        assert ab["p_value_adjusted"] == pytest.approx(0.09375)
        assert ab["correction"] == "holm"
        assert ab["n_comparisons"] == 3

    def test_refused_pairs_untouched_by_correction(self) -> None:
        """arm-c on disjoint tasks: its pairs are REFUSED and contribute
        None to the family; the one tested pair adjusts with m=1."""
        disjoint = ConfigResults(
            config="arm-c",
            completed=[_task(f"u{i}", 1.0) for i in range(3)],
        )
        report = generate_report(
            "karm-exp",
            [self._arm("arm-a", self._A), self._arm("arm-b", self._B), disjoint],
        )
        by_pair = {(c.config_a, c.config_b): c for c in report.comparisons}

        ab = by_pair[("arm-a", "arm-b")]
        assert ab.correction == "holm"
        assert ab.n_comparisons == 1
        assert ab.p_value_adjusted == pytest.approx(ab.p_value)

        for pair in (("arm-a", "arm-c"), ("arm-b", "arm-c")):
            refused = by_pair[pair]
            assert refused.comparable is False
            assert refused.correction == "none"
            assert refused.p_value_adjusted is None
            assert "NOT COMPARABLE" in refused.summary

    def test_streaming_applies_same_correction(self) -> None:
        batch = self._three_arm_report()

        def stream() -> Iterator[tuple[str, Iterator[CompletedTask]]]:
            for label, scores in (
                ("arm-a", self._A),
                ("arm-b", self._B),
                ("arm-c", self._C),
            ):
                yield (
                    label,
                    iter(
                        [_task(t, s) for t, s in zip(self._TASKS, scores)]
                    ),
                )

        streaming = generate_report_streaming("karm-exp", stream())
        assert len(streaming.comparisons) == 3
        for b_c, s_c in zip(batch.comparisons, streaming.comparisons):
            assert s_c.p_value_adjusted == b_c.p_value_adjusted
            assert s_c.correction == b_c.correction == "holm"
            assert s_c.n_comparisons == b_c.n_comparisons == 3
            assert s_c.summary == b_c.summary


# ---------------------------------------------------------------------------
# Cost provenance and comparability (codeprobe-f7rl.35)
# ---------------------------------------------------------------------------


class TestCostProvenance:
    """Coverage/provenance surfacing and comparability gating for costs."""

    def _mixed_coverage_tasks(self) -> list[CompletedTask]:
        """10 completed trials: cost captured on 2 (api_reported), 8 without."""
        with_cost = [
            _task(f"t{i}", 1.0, cost=0.10, cost_source="api_reported")
            for i in range(2)
        ]
        without_cost = [_task(f"t{i}", 1.0) for i in range(2, 10)]
        return with_cost + without_cost

    def _summary(self, label: str, **overrides: object) -> ConfigSummary:
        base: dict[str, object] = dict(
            total_tasks=5,
            completed=5,
            errored=0,
            pass_rate=1.0,
            mean_score=0.8,
            median_score=0.8,
            total_duration_sec=50.0,
            mean_duration_sec=10.0,
            total_cost_usd=1.00,
            total_tokens=1000,
            cost_coverage=1.0,
        )
        base.update(overrides)
        return ConfigSummary(label=label, **base)  # type: ignore[arg-type]

    def test_summary_cost_coverage_and_source_counts(self) -> None:
        """2/10 trials with cost -> coverage 0.2; streaming path agrees."""
        tasks = self._mixed_coverage_tasks()
        s = summarize_config(ConfigResults(config="partial", completed=tasks))

        assert s.cost_coverage == pytest.approx(0.2)
        assert s.cost_source_counts == {"api_reported": 2, "unavailable": 8}
        assert s.total_cost_usd == pytest.approx(0.20)

        stream = summarize_completed_tasks("partial", iter(tasks))
        assert stream.cost_coverage == s.cost_coverage
        assert stream.cost_source_counts == s.cost_source_counts

    def test_full_coverage_summary(self) -> None:
        """Every scorable trial costed -> coverage 1.0, single-source tally."""
        tasks = [
            _task(f"t{i}", 1.0, cost=0.10, cost_source="api_reported")
            for i in range(4)
        ]
        s = summarize_config(ConfigResults(config="full", completed=tasks))
        assert s.cost_coverage == 1.0
        assert s.cost_source_counts == {"api_reported": 4}

    def test_cost_comparable_predicate(self) -> None:
        full_a = self._summary("a")
        full_b = self._summary("b")
        partial = self._summary("p", cost_coverage=0.2)
        assert cost_comparable(full_a, full_b) is True
        assert cost_comparable(full_a, partial) is False
        assert cost_comparable(partial, full_a) is False

    def test_winner_tiebreak_skips_incomparable_cost(self) -> None:
        """Equal means, partial-coverage cheap arm -> speed decides, not cost."""
        cheap_partial = self._summary(
            "cheap-partial",
            total_cost_usd=0.20,
            cost_coverage=0.2,
            mean_duration_sec=20.0,
        )
        full = self._summary(
            "full", total_cost_usd=1.00, mean_duration_sec=10.0
        )
        tied = [0.8, 0.8, 0.8, 0.8, 0.8]
        cmp = compare_configs(
            cheap_partial, full, a_scores=tied, b_scores=tied
        )
        # The undercounted $0.20 total must not crown the partial arm; the
        # tiebreak falls through to speed and the faster full arm wins.
        assert cmp.winner == "full"

        # Control: with full coverage on both, the cheaper arm wins on cost.
        cheap_full = self._summary(
            "cheap-full",
            total_cost_usd=0.20,
            mean_duration_sec=20.0,
        )
        cmp2 = compare_configs(cheap_full, full, a_scores=tied, b_scores=tied)
        assert cmp2.winner == "cheap-full"

    def test_best_cost_efficiency_requires_full_coverage(self) -> None:
        """Partial-coverage lowest-cost arm gets ordinal, not cost-efficiency."""
        best = self._summary("best", mean_score=0.90, total_cost_usd=1.00)
        cheap_partial = self._summary(
            "cheap",
            mean_score=0.85,  # within 10% of best
            total_cost_usd=0.20,
            cost_coverage=0.2,
        )
        ranked = rank_configs([best, cheap_partial])
        assert ranked[0].label == "best"
        assert ranked[1].label == "cheap"
        assert "cost-efficiency" not in ranked[1].recommendation.lower()
        assert "2nd" in ranked[1].recommendation

        # Control: the same arm with full coverage still earns the tag.
        cheap_full = self._summary(
            "cheap", mean_score=0.85, total_cost_usd=0.20
        )
        ranked_full = rank_configs([best, cheap_full])
        assert "cost-efficiency" in ranked_full[1].recommendation.lower()

    def test_text_and_html_show_cost_provenance(self) -> None:
        """Text, HTML and JSON all carry coverage + provenance per arm."""
        task_ids = [f"t{i}" for i in range(10)]
        partial_arm = ConfigResults(
            config="partial-arm",
            completed=[
                _task(
                    tid,
                    1.0,
                    cost=0.10 if i < 2 else None,
                    cost_source="api_reported" if i < 2 else "unavailable",
                )
                for i, tid in enumerate(task_ids)
            ],
        )
        full_arm = ConfigResults(
            config="full-arm",
            completed=[
                _task(tid, 1.0, cost=0.10, cost_source="api_reported")
                for tid in task_ids
            ],
        )
        report = generate_report("cost-prov", [partial_arm, full_arm])

        text = format_text_report(report)
        assert "on 2/10 trials" in text
        assert "not comparable" in text
        assert "$1.00 total (10/10 trials, api_reported)" in text
        assert "Cost note:" in text
        assert "EXCLUDED from winner tiebreaks" in text

        html = format_html_report(report)
        assert "cost on 2/10 trials — not comparable" in html
        assert "(10/10 trials, api_reported)" in html

        data = json.loads(format_json_report(report))
        by_label = {s["label"]: s for s in data["summaries"]}
        assert by_label["partial-arm"]["cost_coverage"] == pytest.approx(0.2)
        assert by_label["partial-arm"]["cost_source_counts"] == {
            "api_reported": 2,
            "unavailable": 8,
        }
        assert by_label["full-arm"]["cost_coverage"] == 1.0

    def test_no_cost_note_when_fully_covered(self) -> None:
        """Identical full-coverage provenance on all arms -> no cost note."""
        task_ids = [f"t{i}" for i in range(4)]
        arms = [
            ConfigResults(
                config=label,
                completed=[
                    _task(tid, 1.0, cost=0.10, cost_source="api_reported")
                    for tid in task_ids
                ],
            )
            for label in ("arm-a", "arm-b")
        ]
        report = generate_report("clean-cost", arms)
        text = format_text_report(report)
        assert "Cost note:" not in text
        assert "not comparable" not in text

    def test_cost_table_errored_arm_shows_dash(self) -> None:
        """All-errored arm renders em-dash in the cost table, not 0%."""
        errored_arm = ConfigResults(
            config="dead-arm",
            completed=[
                _task(f"t{i}", 0.0, status="error", cost=0.05) for i in range(3)
            ],
        )
        report = generate_report("dead-exp", [errored_arm])
        html = format_html_report(report)
        cost_section = html.split('id="cost-efficiency"')[1]
        assert "0%" not in cost_section
        assert "—" in cost_section
