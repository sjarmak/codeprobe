"""Tests for stats.py — task_passed and aggregate pass_rate consistency."""

from __future__ import annotations

from codeprobe.analysis.stats import (
    PASS_THRESHOLD,
    summarize_config,
    summarize_completed_tasks,
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
