"""Honest verdicts, metric-correct CI bars, and small-N labels (codeprobe-f7rl.31).

Covers the three report-surface honesty fixes:

1. Pairwise cards gate the green Winner badge on a clean ``"X wins"``
   verdict — softened verdicts render a warning badge with the verdict text.
2. The ranking-table CI bar plots the summary's PRIMARY metric per the
   ``ConfigSummary`` contract: ``mean_score`` for continuous scorers,
   ``pass_rate`` for binary ones.
3. Small samples (2 <= N < 10) carry the accurate stats-layer warning on
   every surface (text, HTML, CSV) instead of the false "Single run" wording,
   and computed CIs are never suppressed.
"""

from __future__ import annotations

import json
import re

import pytest

from codeprobe.analysis.report import (
    Report,
    format_csv_report,
    format_html_report,
    format_json_report,
    format_text_report,
    generate_report,
)
from codeprobe.models.experiment import CompletedTask, ConfigResults


def _task(task_id: str, score: float, *, duration: float = 10.0) -> CompletedTask:
    return CompletedTask(
        task_id=task_id,
        automated_score=score,
        duration_seconds=duration,
    )


def _arm(config: str, scores: list[float]) -> ConfigResults:
    return ConfigResults(
        config=config,
        completed=[_task(f"t{i}", s) for i, s in enumerate(scores)],
    )


def _ci_point_percents(html: str) -> list[float]:
    return [
        float(m)
        for m in re.findall(r'class="ci-point" style="left:([0-9.]+)%"', html)
    ]


def _softened_report() -> Report:
    """Two arms with a real but tiny, non-significant score difference."""
    a_scores = [0.95, 0.10, 0.85, 0.20, 0.75, 0.30]
    b_scores = [0.90, 0.13, 0.81, 0.24, 0.72, 0.28]
    return generate_report(
        "soft-exp", [_arm("arm-a", a_scores), _arm("arm-b", b_scores)]
    )


def _clean_win_report() -> Report:
    """Two arms separated far enough for a significant, large-effect win."""
    a_scores = [0.90, 0.92, 0.94, 0.96, 0.98, 1.00, 0.88, 0.86]
    b_scores = [0.40, 0.40, 0.40, 0.40, 0.40, 0.40, 0.26, 0.22]
    return generate_report(
        "win-exp", [_arm("alpha", a_scores), _arm("beta", b_scores)]
    )


def _small_n_report() -> Report:
    """Two comparable arms with N=5 scored trials each."""
    a_scores = [0.9, 0.8, 0.85, 0.95, 0.7]
    b_scores = [0.4, 0.5, 0.45, 0.35, 0.6]
    return generate_report(
        "small-exp", [_arm("arm-a", a_scores), _arm("arm-b", b_scores)]
    )


class TestPairwiseVerdictBadges:
    def test_pairwise_card_shows_softened_verdict(self) -> None:
        report = _softened_report()
        (c,) = report.comparisons
        assert c.comparable is True
        assert "not significant" in c.verdict
        assert c.verdict != f"{c.winner} wins"

        html = format_html_report(report)
        assert c.summary in html
        assert "not significant" in html
        assert "Winner:" not in html
        # The softened verdict itself is the badge.
        assert f'<span class="warn-badge">{c.verdict}</span>' in html

    def test_pairwise_card_winner_badge_only_on_clean_win(self) -> None:
        report = _clean_win_report()
        (c,) = report.comparisons
        assert c.verdict == "alpha wins"

        html = format_html_report(report)
        assert "Winner: alpha" in html
        assert '<span class="winner-badge">' in html


class TestCiBarMetric:
    def test_ci_bar_uses_mean_score_for_continuous(self) -> None:
        scores = [0.50, 0.55, 0.60, 0.65, 0.70, 0.52, 0.57, 0.62, 0.67, 0.54]
        report = generate_report("cont-exp", [_arm("cont", scores)])
        (s,) = report.summaries
        assert s.score_type == "continuous"
        assert s.pass_rate == 1.0  # every score >= PASS_THRESHOLD

        html = format_html_report(report)
        (mid,) = _ci_point_percents(html)
        # The point marks mean_score, not the 100% pass rate, and it sits
        # inside its own interval by construction (0.06 absorbs the .1f
        # rendering rounding).
        assert mid == pytest.approx(s.mean_score * 100, abs=0.06)
        assert s.ci_lower * 100 - 0.06 <= mid <= s.ci_upper * 100 + 0.06
        assert "mean score" in html

    def test_ci_bar_uses_pass_rate_for_binary(self) -> None:
        scores = [1.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0]
        report = generate_report("bin-exp", [_arm("bin", scores)])
        (s,) = report.summaries
        assert s.score_type == "binary"

        html = format_html_report(report)
        (mid,) = _ci_point_percents(html)
        assert mid == pytest.approx(s.pass_rate * 100, abs=0.06)
        assert "pass rate" in html


class TestSmallSampleLabels:
    def test_small_n_label_accurate(self) -> None:
        report = _small_n_report()
        html = format_html_report(report)

        assert "Small sample size (N=5)" in html
        assert "Single run" not in html
        # Computed CIs are never suppressed for 2 <= N < 10.
        assert '<div class="ci-bar"' in html
        assert len(_ci_point_percents(html)) == 2
        assert "CI (score diff)" in html

        csv_text = format_csv_report(report)
        assert csv_text.splitlines()[0].startswith("# SMALL SAMPLE")

    def test_text_report_surfaces_small_n(self) -> None:
        text = format_text_report(_small_n_report())
        assert "Small sample size (N=5)" in text
        assert "interpret CIs with caution" in text
        assert "Single run" not in text


class TestJsonVerdict:
    def test_json_comparison_carries_verdict(self) -> None:
        report = _clean_win_report()
        data = json.loads(format_json_report(report))
        (c_json,) = data["comparisons"]
        (c,) = report.comparisons
        assert c_json["verdict"] == c.verdict
        assert c_json["verdict"] == "alpha wins"
        assert c_json["verdict"] in c_json["summary"]
