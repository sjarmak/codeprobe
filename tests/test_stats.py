"""Tests for stats.py — task_passed and aggregate pass_rate consistency."""

from __future__ import annotations

from codeprobe.analysis.stats import (
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

        tasks_a = [
            _task("t1", 1.0, scoring_details={"passed": False}),
            _task("t2", 1.0),
        ]
        tasks_b = [
            _task("t1", 1.0),
            _task("t2", 0.0),
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
        cmp = self._run_compare([0.5, 0.5], [0.5, 0.5])
        assert "effectively tied" in cmp.summary

    def test_real_experiment_numbers_produce_softened_verdict(self) -> None:
        """Regression: the kubernetes-mcp-comparison scenario (N=5, d=0.076)."""
        baseline = [0.75, 0.40, 0.11, 0.71, 0.14]
        with_mcp = [0.71, 0.36, 0.08, 0.71, 0.14]
        cmp = self._run_compare(baseline, with_mcp)
        # score_diff ~0.02, small cohen's d, high p → softened verdict
        assert "nominally ahead" in cmp.summary
