"""Tests for the run-command terminal-summary shaping (codeprobe-9jxx).

The headline mean_score in the run envelope must exclude quota casualties
(executor-stamped automated_score=0.0) so an infrastructure failure does not
drag the published reward toward zero, while structural totals (tasks, cost)
stay over all attempts and a quota_error_count surfaces the exclusion.
"""

from __future__ import annotations

import pytest

from codeprobe.cli.run_cmd import build_run_envelope_summary
from codeprobe.models.experiment import CompletedTask


def test_envelope_summary_excludes_quota_from_mean() -> None:
    results_by_config = {
        "baseline": [
            CompletedTask(task_id="t-001", automated_score=1.0, cost_usd=0.10),
            CompletedTask(task_id="t-002", automated_score=0.5, cost_usd=0.10),
            CompletedTask(
                task_id="t-003",
                automated_score=0.0,
                cost_usd=0.05,
                error_category="quota",
            ),
        ]
    }

    summary_configs, total_tasks, total_cost = build_run_envelope_summary(
        results_by_config
    )

    (cfg,) = summary_configs
    # Mean over the two real trials only — the 0.0 quota stub is excluded.
    assert cfg["mean_score"] == pytest.approx((1.0 + 0.5) / 2)
    assert cfg["perfect"] == 1
    # Structural totals stay over all attempts; quota count is surfaced.
    assert cfg["tasks"] == 3
    assert cfg["quota_error_count"] == 1
    assert total_tasks == 3
    # Cost stays over all attempts — quota trials still cost real money.
    assert cfg["cost_usd"] == pytest.approx(0.25)
    assert total_cost == pytest.approx(0.25)


def test_envelope_summary_all_quota_yields_zero_mean() -> None:
    results_by_config = {
        "baseline": [
            CompletedTask(
                task_id="t-001", automated_score=0.0, error_category="quota"
            ),
            CompletedTask(
                task_id="t-002", automated_score=0.0, error_category="quota"
            ),
        ]
    }

    (cfg,), total_tasks, _ = build_run_envelope_summary(results_by_config)

    assert cfg["mean_score"] == 0.0
    assert cfg["perfect"] == 0
    assert cfg["quota_error_count"] == 2
    assert cfg["tasks"] == 2
    assert total_tasks == 2


def test_envelope_summary_no_quota_is_unchanged() -> None:
    """Regression guard: with no quota casualties the mean is over all trials."""
    results_by_config = {
        "baseline": [
            CompletedTask(task_id="t-001", automated_score=1.0, cost_usd=0.10),
            CompletedTask(task_id="t-002", automated_score=0.0, cost_usd=0.10),
        ]
    }

    (cfg,), _, _ = build_run_envelope_summary(results_by_config)

    assert cfg["mean_score"] == pytest.approx(0.5)
    assert cfg["quota_error_count"] == 0
    assert cfg["tasks"] == 2
