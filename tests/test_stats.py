"""Tests for stats.py — task_passed and aggregate pass_rate consistency."""

from __future__ import annotations

import pytest

from codeprobe.analysis.stats import (
    holm_adjusted,
    is_scorable_run,
    partition_reward_population,
    summarize_completed_tasks,
    summarize_config,
    task_passed,
)
from codeprobe.models.experiment import CompletedTask, ConfigResults


def _task(
    task_id: str,
    score: float,
    *,
    scoring_details: dict | None = None,
) -> CompletedTask:
    return CompletedTask(
        task_id=task_id,
        automated_score=score,
        status="completed",
        duration_seconds=10.0,
        cost_usd=0.05,
        scoring_details=scoring_details or {},
    )


class TestTaskPassed:
    """Unit tests for the task_passed helper."""

    def test_explicit_false_overrides_high_score(self) -> None:
        """score=1.0 but scoring_details['passed']=False → not passed."""
        t = _task("t1", 1.0, scoring_details={"passed": False})
        assert task_passed(t) is False

    def test_explicit_true_overrides_low_score(self) -> None:
        """score=0.0 but scoring_details['passed']=True → passed."""
        t = _task("t1", 0.0, scoring_details={"passed": True})
        assert task_passed(t) is True

    def test_fallback_to_score_threshold_pass(self) -> None:
        t = _task("t1", 1.0)
        assert task_passed(t) is True

    def test_fallback_to_score_threshold_fail(self) -> None:
        t = _task("t1", 0.0)
        assert task_passed(t) is False

    def test_string_false_round_tripped(self) -> None:
        """JSON round-trip can turn False into 'false' string."""
        t = _task("t1", 1.0, scoring_details={"passed": "false"})
        assert task_passed(t) is False

    def test_string_true_round_tripped(self) -> None:
        t = _task("t1", 0.0, scoring_details={"passed": "true"})
        assert task_passed(t) is True


class TestAggregatePassRateConsistency:
    """Aggregate pass_rate must respect scoring_details['passed']."""

    def test_summarize_config_explicit_false_high_score(self) -> None:
        """Task with score=1.0 and passed=False must NOT count as passed."""
        tasks = [_task("t1", 1.0, scoring_details={"passed": False})]
        cr = ConfigResults(config="cfg", completed=tasks)
        summary = summarize_config(cr)
        assert summary.pass_rate == 0.0

    def test_summarize_config_explicit_true_low_score(self) -> None:
        """Task with score=0.0 and passed=True must count as passed."""
        tasks = [_task("t1", 0.0, scoring_details={"passed": True})]
        cr = ConfigResults(config="cfg", completed=tasks)
        summary = summarize_config(cr)
        assert summary.pass_rate == 1.0

    def test_summarize_completed_tasks_explicit_false_high_score(self) -> None:
        """Streaming variant: score=1.0 + passed=False → pass_rate=0.0."""
        tasks = [_task("t1", 1.0, scoring_details={"passed": False})]
        summary = summarize_completed_tasks("cfg", iter(tasks))
        assert summary.pass_rate == 0.0

    def test_summarize_completed_tasks_explicit_true_low_score(self) -> None:
        """Streaming variant: score=0.0 + passed=True → pass_rate=1.0."""
        tasks = [_task("t1", 0.0, scoring_details={"passed": True})]
        summary = summarize_completed_tasks("cfg", iter(tasks))
        assert summary.pass_rate == 1.0

    def test_mixed_tasks_correct_rate(self) -> None:
        """Mix of explicit and fallback tasks."""
        tasks = [
            _task("t1", 1.0, scoring_details={"passed": False}),  # NOT passed
            _task("t2", 1.0),  # passed (fallback)
            _task("t3", 0.0, scoring_details={"passed": True}),  # passed
            _task("t4", 0.0),  # NOT passed (fallback)
        ]
        cr = ConfigResults(config="cfg", completed=tasks)
        summary = summarize_config(cr)
        assert summary.pass_rate == 0.5  # 2 out of 4

    def test_mixed_tasks_streaming_correct_rate(self) -> None:
        tasks = [
            _task("t1", 1.0, scoring_details={"passed": False}),
            _task("t2", 1.0),
            _task("t3", 0.0, scoring_details={"passed": True}),
            _task("t4", 0.0),
        ]
        summary = summarize_completed_tasks("cfg", iter(tasks))
        assert summary.pass_rate == 0.5


class TestMcNemarConsistencyWithTaskPassed:
    """McNemar's test must agree with pass_rate when scoring_details['passed']
    overrides the automated_score threshold.

    Regression: a task with score=1.0 and scoring_details={'passed': False}
    was counted as pass by McNemar (via PASS_THRESHOLD) but as fail by
    pass_rate (via task_passed). The fix is in report.py — generate_report
    now passes binary scores derived from task_passed() to compare_configs.
    """

    def test_mcnemar_agrees_with_pass_rate_on_explicit_false(self) -> None:
        """Two configs, one task has score=1.0 + passed=False.

        Config A: task t1 score=1.0, passed=False → fail via task_passed
        Config A: task t2 score=1.0                → pass via task_passed
        Config B: task t1 score=1.0                → pass via task_passed
        Config B: task t2 score=0.0                → fail via task_passed

        When generate_report builds binary scores from task_passed:
        A = [0.0, 1.0], B = [1.0, 0.0] — discordant pairs = 2
        pass_rate_a = 0.5, pass_rate_b = 0.5 — tied.

        If raw automated_score were used instead:
        A = [1.0, 1.0], B = [1.0, 0.0] — only 1 discordant pair
        And pass_rate_a would wrongly look like 1.0 instead of 0.5.
        """
        from codeprobe.analysis.report import generate_report
        from codeprobe.models.experiment import ConfigResults

        # t3/t4 are concordant filler pairs: they keep both pass rates at 0.5
        # and lift the shared-task count past the _MIN_PAIRED_TASKS floor so
        # the comparison is not refused (codeprobe-f7rl.8).
        tasks_a = [
            _task("t1", 1.0, scoring_details={"passed": False}),
            _task("t2", 1.0),
            _task("t3", 1.0),
            _task("t4", 0.0),
        ]
        tasks_b = [
            _task("t1", 1.0),
            _task("t2", 0.0),
            _task("t3", 1.0),
            _task("t4", 0.0),
        ]
        cr_a = ConfigResults(config="cfg-a", completed=tasks_a)
        cr_b = ConfigResults(config="cfg-b", completed=tasks_b)

        report = generate_report("test", [cr_a, cr_b])

        # Both should have pass_rate=0.5
        summary_map = {s.label: s for s in report.summaries}
        assert summary_map["cfg-a"].pass_rate == 0.5
        assert summary_map["cfg-b"].pass_rate == 0.5

        # The pairwise comparison should see 2 discordant pairs (both swap),
        # yielding p_value=1.0 (no significant difference).
        # If raw scores were used, there would be only 1 discordant pair.
        assert len(report.comparisons) == 1
        cmp = report.comparisons[0]
        # With binary scores via task_passed, scores passed to compare_configs
        # are [0.0, 1.0] vs [1.0, 0.0] — both are binary.
        assert cmp.effect_size_method == "cliffs_delta"
        # p_value should be 1.0 (2 discordant pairs, perfectly balanced)
        assert cmp.p_value == 1.0


# ---------------------------------------------------------------------------
# Score-type-aware CI + effect size (0.5.2 fix for continuous scorers)
# ---------------------------------------------------------------------------


class TestScoreTypeDetection:
    """summarize_config infers score_type and picks the right CI."""

    def test_continuous_scores_get_mean_score_ci(self) -> None:
        """Scores like F1 get a t/normal CI on mean_score, not Wilson."""
        cr = ConfigResults(
            config="mcp",
            completed=[
                _task("t1", 0.75),
                _task("t2", 0.40),
                _task("t3", 0.11),
                _task("t4", 0.71),
                _task("t5", 0.14),
            ],
        )
        s = summarize_config(cr)
        assert s.score_type == "continuous"
        # Wilson on 5/5 would give ~[0.566, 1.0]. Mean-score CI on these
        # scores gives something near the mean (~0.42) with spread reflecting
        # the variance — definitively different from the Wilson output.
        assert s.ci_lower < 0.5  # mean-score CI is centred near 0.42
        assert s.ci_upper < 0.9  # not pinned to 1.0 like Wilson
        assert s.mean_score > s.ci_lower and s.mean_score < s.ci_upper

    def test_binary_scores_keep_wilson_ci(self) -> None:
        """Pure 0/1 scores get Wilson CI on pass_rate (backwards compat)."""
        cr = ConfigResults(
            config="binary",
            completed=[
                _task("t1", 1.0),
                _task("t2", 1.0),
                _task("t3", 0.0),
                _task("t4", 1.0),
            ],
        )
        s = summarize_config(cr)
        assert s.score_type == "binary"
        # Wilson for 3/4 ≈ [0.30, 0.95]; mean-score CI on [1,1,0,1] would
        # give a tighter interval. We just assert the Wilson shape.
        assert 0.25 < s.ci_lower < 0.40
        assert 0.85 < s.ci_upper < 1.0


class TestComparePairwiseContinuousRouting:
    """compare_configs routes to Wilcoxon+Cohen's d when scores aren't 0/1."""

    def test_continuous_picks_cohens_d(self) -> None:
        from codeprobe.analysis.stats import compare_configs

        a_cr = ConfigResults(
            config="a",
            completed=[_task("t1", 0.8), _task("t2", 0.6), _task("t3", 0.9)],
        )
        b_cr = ConfigResults(
            config="b",
            completed=[_task("t1", 0.2), _task("t2", 0.3), _task("t3", 0.1)],
        )
        a_sum = summarize_config(a_cr)
        b_sum = summarize_config(b_cr)
        cmp = compare_configs(
            a_sum, b_sum,
            a_scores=[0.8, 0.6, 0.9],
            b_scores=[0.2, 0.3, 0.1],
        )
        assert cmp.effect_size_method == "cohens_d"
        # Cohen's d should be clearly positive (a much larger than b).
        assert cmp.effect_size is not None and cmp.effect_size > 1.0

    def test_binary_keeps_cliffs_delta(self) -> None:
        from codeprobe.analysis.stats import compare_configs

        a_cr = ConfigResults(
            config="a",
            completed=[_task("t1", 1.0), _task("t2", 1.0), _task("t3", 0.0)],
        )
        b_cr = ConfigResults(
            config="b",
            completed=[_task("t1", 0.0), _task("t2", 1.0), _task("t3", 0.0)],
        )
        cmp = compare_configs(
            summarize_config(a_cr), summarize_config(b_cr),
            a_scores=[1.0, 1.0, 0.0],
            b_scores=[0.0, 1.0, 0.0],
        )
        assert cmp.effect_size_method == "cliffs_delta"


class TestVerdictSoftening:
    """Summary text softens the verdict when the effect is small or p is high."""

    def _run_compare(self, a_scores, b_scores):
        from codeprobe.analysis.stats import compare_configs
        from codeprobe.models.experiment import ConfigResults

        a_cr = ConfigResults(
            config="a",
            completed=[_task(f"t{i}", s) for i, s in enumerate(a_scores)],
        )
        b_cr = ConfigResults(
            config="b",
            completed=[_task(f"t{i}", s) for i, s in enumerate(b_scores)],
        )
        return compare_configs(
            summarize_config(a_cr), summarize_config(b_cr),
            a_scores=list(a_scores), b_scores=list(b_scores),
        )

    def test_large_effect_with_power_says_wins(self) -> None:
        """Consistent large gap across enough samples → unqualified winner."""
        # N=8, unambiguous separation in every paired sample
        a = [0.90, 0.88, 0.92, 0.85, 0.87, 0.93, 0.89, 0.91]
        b = [0.10, 0.12, 0.15, 0.08, 0.18, 0.11, 0.14, 0.09]
        cmp = self._run_compare(a, b)
        assert "a wins" in cmp.summary
        assert "nominally" not in cmp.summary

    def test_small_effect_softens_verdict(self) -> None:
        """Noisy data with a tiny gap → softened verdict.

        The gap (~0.02) clears the 0.01 tied threshold, but high within-
        config variance keeps Cohen's d < 0.2, which should trigger the
        "nominally ahead (small effect)" wording.
        """
        a = [0.95, 0.10, 0.85, 0.20, 0.75, 0.30]
        b = [0.93, 0.08, 0.83, 0.18, 0.72, 0.28]
        cmp = self._run_compare(a, b)
        assert "nominally ahead" in cmp.summary
        # Should NOT say "wins" unqualified
        assert " a wins" not in cmp.summary
        assert " b wins" not in cmp.summary

    def test_tied_scores_report_tied(self) -> None:
        # 3 paired scores: at the _MIN_PAIRED_TASKS floor so the pair is
        # comparable and the tied verdict (not a refusal) is exercised.
        cmp = self._run_compare([0.5, 0.5, 0.5], [0.5, 0.5, 0.5])
        assert "effectively tied" in cmp.summary

    def test_real_experiment_numbers_produce_softened_verdict(self) -> None:
        """Regression: the kubernetes-mcp-comparison scenario (N=5, d=0.076)."""
        baseline = [0.75, 0.40, 0.11, 0.71, 0.14]
        with_mcp = [0.71, 0.36, 0.08, 0.71, 0.14]
        cmp = self._run_compare(baseline, with_mcp)
        # score_diff ~0.02, small cohen's d, high p → softened verdict
        assert "nominally ahead" in cmp.summary


class TestRefusedVerdicts:
    """compare_configs REFUSES incomparable arms instead of picking a winner
    (locked decision 6, epic codeprobe-f7rl): disjoint task sets or fewer than
    _MIN_PAIRED_TASKS shared tasks produce comparable=False and no verdict."""

    def _summaries(self, a_scores, b_scores):
        a_cr = ConfigResults(
            config="a",
            completed=[_task(f"a{i}", s) for i, s in enumerate(a_scores)],
        )
        b_cr = ConfigResults(
            config="b",
            completed=[_task(f"b{i}", s) for i, s in enumerate(b_scores)],
        )
        return summarize_config(a_cr), summarize_config(b_cr)

    def test_disjoint_arms_refused(self) -> None:
        """No shared tasks (a_scores=None) → refusal, never '+65% → A wins'."""
        from codeprobe.analysis.stats import compare_configs

        a_sum, b_sum = self._summaries([0.9, 0.8, 0.85], [0.2, 0.3, 0.25])
        cmp = compare_configs(a_sum, b_sum, a_scores=None, b_scores=None)
        assert cmp.comparable is False
        assert cmp.winner == ""
        assert "NOT COMPARABLE" in cmp.summary
        assert "disjoint task sets" in cmp.refusal_reason
        assert "wins" not in cmp.summary
        assert cmp.p_value is None
        assert cmp.effect_size is None

    def test_below_floor_refused(self) -> None:
        """2 paired scores → refused with the floor reason."""
        from codeprobe.analysis.stats import compare_configs

        a_sum, b_sum = self._summaries([0.9, 0.8], [0.2, 0.3])
        cmp = compare_configs(
            a_sum, b_sum, a_scores=[0.9, 0.8], b_scores=[0.2, 0.3]
        )
        assert cmp.comparable is False
        assert cmp.winner == ""
        assert "below the 3-task paired-comparison floor" in cmp.refusal_reason
        assert "NOT COMPARABLE" in cmp.summary

    def test_at_floor_comparable(self) -> None:
        """3 paired scores → comparable, normal verdict chain."""
        from codeprobe.analysis.stats import compare_configs

        a_sum, b_sum = self._summaries([0.9, 0.8, 0.85], [0.2, 0.3, 0.25])
        cmp = compare_configs(
            a_sum, b_sum, a_scores=[0.9, 0.8, 0.85], b_scores=[0.2, 0.3, 0.25]
        )
        assert cmp.comparable is True
        assert cmp.refusal_reason == ""
        assert cmp.winner == "a"
        assert "NOT COMPARABLE" not in cmp.summary

    def test_refused_keeps_reference_diffs(self) -> None:
        """Refusal keeps score/cost/speed diffs as reference-only data."""
        from codeprobe.analysis.stats import compare_configs

        a_sum, b_sum = self._summaries([0.9, 0.8, 0.85], [0.2, 0.3, 0.25])
        cmp = compare_configs(a_sum, b_sum, a_scores=None, b_scores=None)
        assert cmp.comparable is False
        assert cmp.score_diff == pytest.approx(
            a_sum.mean_score - b_sum.mean_score
        )
        assert cmp.cost_diff is not None
        assert cmp.speed_diff == pytest.approx(
            a_sum.mean_duration_sec - b_sum.mean_duration_sec
        )


def _quota_task(task_id: str, *, duration: float = 99.0) -> CompletedTask:
    """A quota-errored infrastructure casualty: the executor stamps it
    automated_score=0.0 and error_category='quota' (codeprobe-a8r)."""
    return CompletedTask(
        task_id=task_id,
        automated_score=0.0,
        status="error",
        duration_seconds=duration,
        cost_usd=0.05,
        error_category="quota",
    )


def _real_task(
    task_id: str, score: float, *, duration: float = 10.0
) -> CompletedTask:
    return CompletedTask(
        task_id=task_id,
        automated_score=score,
        status="completed",
        duration_seconds=duration,
        cost_usd=0.05,
    )


def _error_task(task_id: str, *, duration: float = 1.0) -> CompletedTask:
    """A non-executed run (status=="error", non-quota): the agent never ran
    (e.g. invalid model token / crash). Stamped automated_score=0.0 by the
    executor; excluded from scoring but not a quota casualty (codeprobe-h3j4)."""
    return CompletedTask(
        task_id=task_id,
        automated_score=0.0,
        status="error",
        duration_seconds=duration,
        error_category="agent",
    )


def _failed_task(task_id: str, *, duration: float = 5.0) -> CompletedTask:
    """A terminal agent failure (status=="failed"): the agent ran to a
    protocol stop condition and the 0.0 is a REAL measurement, so it stays in
    the reward population (codeprobe-8up / codeprobe-h3j4)."""
    return CompletedTask(
        task_id=task_id,
        automated_score=0.0,
        status="failed",
        duration_seconds=duration,
    )


class TestIsScorableRun:
    """is_scorable_run keeps real measurements, drops non-executed runs."""

    def test_completed_is_scorable(self) -> None:
        assert is_scorable_run(_real_task("c1", 1.0)) is True

    def test_failed_stays_in_population(self) -> None:
        # The opposite-bug guard: a terminal "failed" run is a real 0.0
        # measurement and must NOT be excluded (that would hide real failures).
        assert is_scorable_run(_failed_task("f1")) is True

    def test_error_is_not_scorable(self) -> None:
        assert is_scorable_run(_error_task("e1")) is False

    def test_quota_casualty_is_not_scorable(self) -> None:
        assert is_scorable_run(_quota_task("q1")) is False

    def test_failed_run_counts_toward_mean(self) -> None:
        # A completed 1.0 and a failed 0.0 average to 0.5 over 2 scored runs;
        # an errored run is excluded entirely.
        results = ConfigResults(
            config="c",
            completed=[_real_task("c1", 1.0), _failed_task("f1"), _error_task("e1")],
        )
        summary = summarize_config(results)
        assert summary.scored_count == 2
        assert summary.errored_count == 1
        assert summary.mean_score == 0.5


class TestQuotaExclusion:
    """Quota-errored trials must NOT contaminate the reward population.

    Regression (codeprobe-a8r / DEEP_AUDIT 2026-06-15 CRITICAL #1): the
    executor assigns automated_score=0.0 to quota casualties, and the
    summarizers rolled that 0.0 into mean_score/median_score/pass-rate/CIs,
    contradicting quota_error_count's own contract (codeprobe-9xrl). The
    fix excludes error_category=='quota' trials from scores, durations, and
    pass-rate while still counting them in quota_error_count.
    """

    # K=2 quota casualties + M=3 real trials (scores 1.0, 1.0, 0.0).
    # Reward population: mean=2/3, median=1.0, pass_rate=2/3.
    # If quota leaked in: mean would be 2/5=0.4, pass_rate 2/5=0.4.
    def _mixed(self) -> list[CompletedTask]:
        return [
            _quota_task("q1"),
            _real_task("r1", 1.0),
            _quota_task("q2"),
            _real_task("r2", 1.0),
            _real_task("r3", 0.0),
        ]

    def test_summarize_config_mean_excludes_quota(self) -> None:
        # A1: mean over the M=3 real trials only.
        cr = ConfigResults(config="cfg", completed=self._mixed())
        s = summarize_config(cr)
        assert s.mean_score == 2 / 3
        assert s.median_score == 1.0
        assert s.pass_rate == 2 / 3
        # A2: quota casualties still surfaced.
        assert s.quota_error_count == 2
        # Durations exclude the inflated quota wall-time (99.0 each).
        assert s.total_duration_sec == 30.0
        assert s.mean_duration_sec == 10.0
        # Structural counts keep all 5 trials.
        assert s.total_tasks == 5
        assert s.errored == 2

    def test_summarize_completed_tasks_mean_excludes_quota(self) -> None:
        # A1/A2 for the streaming summarizer — must match summarize_config.
        s = summarize_completed_tasks("cfg", iter(self._mixed()))
        assert s.mean_score == 2 / 3
        assert s.median_score == 1.0
        assert s.pass_rate == 2 / 3
        assert s.quota_error_count == 2
        assert s.total_duration_sec == 30.0
        assert s.mean_duration_sec == 10.0
        assert s.total_tasks == 5
        assert s.errored == 2

    def test_both_summarizers_agree_on_quota_mix(self) -> None:
        cr = ConfigResults(config="cfg", completed=self._mixed())
        batch = summarize_config(cr)
        stream = summarize_completed_tasks("cfg", iter(self._mixed()))
        assert batch.mean_score == stream.mean_score
        assert batch.pass_rate == stream.pass_rate
        assert batch.quota_error_count == stream.quota_error_count
        assert batch.mean_duration_sec == stream.mean_duration_sec

    def test_all_quota_reports_no_reward_signal(self) -> None:
        # Edge case: every trial was a quota casualty. No reward population →
        # zeroed stats, but the count is still surfaced (no ZeroDivision /
        # StatisticsError crash).
        tasks = [_quota_task("q1"), _quota_task("q2")]
        cr = ConfigResults(config="cfg", completed=tasks)
        for s in (
            summarize_config(cr),
            summarize_completed_tasks("cfg", iter(tasks)),
        ):
            assert s.mean_score == 0.0
            assert s.median_score == 0.0
            assert s.pass_rate == 0.0
            assert s.quota_error_count == 2
            assert s.total_tasks == 2

    def test_no_quota_is_unchanged(self) -> None:
        # Backwards-compat: with zero quota trials, behaviour is identical
        # to the pre-fix summarizer.
        tasks = [_real_task("r1", 1.0), _real_task("r2", 0.0)]
        cr = ConfigResults(config="cfg", completed=tasks)
        s = summarize_config(cr)
        assert s.mean_score == 0.5
        assert s.pass_rate == 0.5
        assert s.quota_error_count == 0

    def test_partition_reward_population_splits_and_counts(self) -> None:
        # The shared SSOT helper that the published-mean paths route through
        # (codeprobe-9jxx): real trials in order, quota + errored counts returned.
        reward, quota_count, errored_count = partition_reward_population(self._mixed())
        assert [t.task_id for t in reward] == ["r1", "r2", "r3"]
        assert quota_count == 2
        # The 2 quota casualties are status=="error", so errored_count == 2.
        assert errored_count == 2

    def test_partition_reward_population_no_quota(self) -> None:
        tasks = [_real_task("r1", 1.0), _real_task("r2", 0.0)]
        reward, quota_count, errored_count = partition_reward_population(tasks)
        assert reward == tasks
        assert quota_count == 0
        assert errored_count == 0

    def test_partition_reward_population_all_quota(self) -> None:
        tasks = [_quota_task("q1"), _quota_task("q2")]
        reward, quota_count, errored_count = partition_reward_population(tasks)
        assert reward == []
        assert quota_count == 2
        assert errored_count == 2

    def test_partition_reward_population_excludes_nonquota_errors(self) -> None:
        # codeprobe-h3j4: a non-executed run (status=="error", NOT quota) is
        # excluded from the reward population and counted as errored, but is
        # NOT a quota casualty.
        tasks = [
            _real_task("r1", 1.0),
            _error_task("e1"),
            _real_task("r2", 0.0),
        ]
        reward, quota_count, errored_count = partition_reward_population(tasks)
        assert [t.task_id for t in reward] == ["r1", "r2"]
        assert quota_count == 0
        assert errored_count == 1

    def test_compare_configs_paired_scores_exclude_quota(self) -> None:
        # Step 2: the paired score lists feeding compare_configs's hypothesis
        # tests must omit quota casualties too, so the tests match the
        # reward population the summaries report.
        from codeprobe.analysis.report import _tee_task_scores

        sink: dict[str, float] = {}
        consumed = list(_tee_task_scores(iter(self._mixed()), sink))
        # All trials still flow through (so the summarizer counts quota)...
        assert len(consumed) == 5
        # ...but only the 3 real trials land in the paired-score sink.
        assert set(sink) == {"r1", "r2", "r3"}


class TestDistinctTaskCount:
    """distinct_task_count is the repeat-safe N (codeprobe-f7rl.9)."""

    def test_summarize_config_counts_unique_task_ids(self) -> None:
        tasks = [_task("t1", 1.0), _task("t1", 0.0), _task("t2", 1.0)]
        s = summarize_config(ConfigResults(config="cfg", completed=tasks))
        assert s.distinct_task_count == 2

    def test_streaming_counts_unique_task_ids(self) -> None:
        tasks = [_task("t1", 1.0), _task("t1", 0.0), _task("t2", 1.0)]
        s = summarize_completed_tasks("cfg", iter(tasks))
        assert s.distinct_task_count == 2

    def test_empty_input_distinct_zero(self) -> None:
        batch = summarize_config(ConfigResults(config="cfg", completed=[]))
        stream = summarize_completed_tasks("cfg", iter([]))
        assert batch.distinct_task_count == 0
        assert stream.distinct_task_count == 0

    def test_repeats_do_not_fake_completeness(self) -> None:
        """6 trials over 2 of 4 expected tasks → partial in BOTH summarizers.

        The old trial-count check saw 6 > 4 and reported complete.
        """
        trials = [_task(f"t{i}", 1.0) for i in range(2) for _ in range(3)]
        batch = summarize_config(
            ConfigResults(config="cfg", completed=trials), total_tasks=4
        )
        stream = summarize_completed_tasks("cfg", iter(trials), total_tasks=4)
        for s in (batch, stream):
            assert s.distinct_task_count == 2
            assert s.is_partial is True

    def test_repeats_do_not_fake_partiality(self) -> None:
        """All expected tasks covered by repeats → NOT partial."""
        trials = [_task(f"t{i}", 1.0) for i in range(2) for _ in range(3)]
        batch = summarize_config(
            ConfigResults(config="cfg", completed=trials), total_tasks=2
        )
        stream = summarize_completed_tasks("cfg", iter(trials), total_tasks=2)
        for s in (batch, stream):
            assert s.distinct_task_count == 2
            assert s.is_partial is False


class TestHolmAdjusted:
    """holm_adjusted — pure step-down correction (codeprobe-f7rl.10)."""

    def test_known_vector(self) -> None:
        """[0.01, 0.04, 0.03]: sorted multipliers 3,2,1 with monotone
        enforcement give [0.03, 0.06, 0.06]."""
        assert holm_adjusted([0.01, 0.04, 0.03]) == pytest.approx(
            [0.03, 0.06, 0.06]
        )

    def test_none_entries_pass_through_and_reduce_m(self) -> None:
        """None (untested/refused pair) keeps its position; m counts only
        tested entries, so [0.01, None, 0.04] adjusts with m=2."""
        result = holm_adjusted([0.01, None, 0.04])
        assert result[1] is None
        assert result[0] == pytest.approx(0.02)  # 2 * 0.01
        assert result[2] == pytest.approx(0.04)  # max(0.02, 1 * 0.04)

    def test_single_p_unchanged(self) -> None:
        assert holm_adjusted([0.03]) == pytest.approx([0.03])

    def test_monotone_and_clamped(self) -> None:
        """Adjusted values never decrease in p-rank order and never exceed
        1.0."""
        raw = [0.9, 0.01, 0.5, 0.04]
        result = holm_adjusted(raw)
        assert all(p is not None and p <= 1.0 for p in result)
        ranked = [adj for _, adj in sorted(zip(raw, result))]
        assert ranked == sorted(ranked)
        assert result[0] == 1.0  # 4 * 0.9 clamps

    def test_empty_and_all_none(self) -> None:
        assert holm_adjusted([]) == []
        assert holm_adjusted([None, None]) == [None, None]
