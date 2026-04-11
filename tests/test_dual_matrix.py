"""Unit tests for dual_matrix() and its integration with report formatters."""

from __future__ import annotations

import json

import pytest

from codeprobe.analysis.dual import DualMatrix, dual_matrix
from codeprobe.analysis.report import (
    Report,
    format_html_report,
    format_json_report,
    format_text_report,
    generate_report,
)
from codeprobe.models.experiment import CompletedTask, ConfigResults


def _dual_task(
    task_id: str,
    *,
    passed_direct: bool,
    passed_artifact: bool,
) -> CompletedTask:
    """Build a CompletedTask with dual scoring details."""
    return CompletedTask(
        task_id=task_id,
        automated_score=1.0 if (passed_direct and passed_artifact) else 0.0,
        duration_seconds=10.0,
        cost_usd=0.01,
        scoring_details={
            "score_direct": 1.0 if passed_direct else 0.0,
            "score_artifact": 1.0 if passed_artifact else 0.0,
            "passed_direct": passed_direct,
            "passed_artifact": passed_artifact,
            "scoring_policy": "gate",
        },
    )


def _non_dual_task(task_id: str) -> CompletedTask:
    """Build a CompletedTask without dual scoring."""
    return CompletedTask(
        task_id=task_id,
        automated_score=1.0,
        duration_seconds=5.0,
        cost_usd=0.005,
        scoring_details={},
    )


# ---------------------------------------------------------------------------
# dual_matrix() unit tests
# ---------------------------------------------------------------------------


class TestDualMatrix:
    def test_all_four_quadrants(self) -> None:
        tasks = [
            _dual_task("t1", passed_direct=True, passed_artifact=True),
            _dual_task("t2", passed_direct=True, passed_artifact=False),
            _dual_task("t3", passed_direct=False, passed_artifact=True),
            _dual_task("t4", passed_direct=False, passed_artifact=False),
        ]
        result = dual_matrix(tasks)
        assert result is not None
        assert result.both_pass == 1
        assert result.code_only_pass == 1
        assert result.artifact_only_pass == 1
        assert result.neither_pass == 1
        assert result.total == 4

    def test_zero_dual_tasks_returns_none(self) -> None:
        tasks = [_non_dual_task("t1"), _non_dual_task("t2")]
        result = dual_matrix(tasks)
        assert result is None

    def test_empty_list_returns_none(self) -> None:
        result = dual_matrix([])
        assert result is None

    def test_mixed_dual_and_non_dual_only_counts_dual(self) -> None:
        tasks = [
            _dual_task("t1", passed_direct=True, passed_artifact=True),
            _non_dual_task("t2"),
            _dual_task("t3", passed_direct=False, passed_artifact=False),
            _non_dual_task("t4"),
        ]
        result = dual_matrix(tasks)
        assert result is not None
        assert result.total == 2
        assert result.both_pass == 1
        assert result.neither_pass == 1

    def test_percentages_sum_to_100(self) -> None:
        tasks = [
            _dual_task("t1", passed_direct=True, passed_artifact=True),
            _dual_task("t2", passed_direct=True, passed_artifact=False),
            _dual_task("t3", passed_direct=False, passed_artifact=True),
        ]
        result = dual_matrix(tasks)
        assert result is not None
        total_pct = (
            result.both_pass_pct
            + result.code_only_pass_pct
            + result.artifact_only_pass_pct
            + result.neither_pass_pct
        )
        assert abs(total_pct - 100.0) < 0.01

    def test_percentages_zero_when_total_zero(self) -> None:
        # DualMatrix with total=0 should not divide by zero
        m = DualMatrix(
            both_pass=0,
            code_only_pass=0,
            artifact_only_pass=0,
            neither_pass=0,
            total=0,
        )
        assert m.both_pass_pct == 0.0
        assert m.code_only_pass_pct == 0.0
        assert m.artifact_only_pass_pct == 0.0
        assert m.neither_pass_pct == 0.0


# ---------------------------------------------------------------------------
# Report rendering tests
# ---------------------------------------------------------------------------


def _make_report_with_dual_tasks() -> Report:
    """Build a Report with dual-scored tasks."""
    tasks = [
        _dual_task("t1", passed_direct=True, passed_artifact=True),
        _dual_task("t2", passed_direct=True, passed_artifact=False),
        _dual_task("t3", passed_direct=False, passed_artifact=True),
        _dual_task("t4", passed_direct=False, passed_artifact=False),
    ]
    config_results = [ConfigResults(config="claude-opus", completed=tasks)]
    return generate_report("test-experiment", config_results)


def _make_report_without_dual_tasks() -> Report:
    """Build a Report without dual-scored tasks."""
    tasks = [_non_dual_task("t1"), _non_dual_task("t2")]
    config_results = [ConfigResults(config="claude-opus", completed=tasks)]
    return generate_report("test-experiment", config_results)


class TestTextReportDualMatrix:
    def test_includes_matrix_when_dual_tasks(self) -> None:
        report = _make_report_with_dual_tasks()
        text = format_text_report(report)
        assert "### Dual Verification Matrix" in text
        assert "Code Pass" in text
        assert "Code Fail" in text
        assert "Artifact Pass" in text
        assert "Artifact Fail" in text

    def test_excludes_matrix_when_no_dual_tasks(self) -> None:
        report = _make_report_without_dual_tasks()
        text = format_text_report(report)
        assert "Dual Verification Matrix" not in text


class TestJsonReportDualMatrix:
    def test_includes_dual_matrix_key_when_dual_tasks(self) -> None:
        report = _make_report_with_dual_tasks()
        raw = format_json_report(report)
        data = json.loads(raw)
        assert "dual_matrix" in data
        dm = data["dual_matrix"]
        assert dm["both_pass"]["count"] == 1
        assert dm["code_only_pass"]["count"] == 1
        assert dm["artifact_only_pass"]["count"] == 1
        assert dm["neither_pass"]["count"] == 1
        assert dm["total"] == 4
        # Percentages present
        assert dm["both_pass"]["pct"] == pytest.approx(25.0)

    def test_excludes_dual_matrix_key_when_no_dual_tasks(self) -> None:
        report = _make_report_without_dual_tasks()
        raw = format_json_report(report)
        data = json.loads(raw)
        assert "dual_matrix" not in data


class TestHtmlReportDualMatrix:
    def test_includes_matrix_card_when_dual_tasks(self) -> None:
        report = _make_report_with_dual_tasks()
        html = format_html_report(report)
        assert "Dual Verification Matrix" in html
        assert "Code Pass" in html
        assert "Artifact Fail" in html

    def test_excludes_matrix_card_when_no_dual_tasks(self) -> None:
        report = _make_report_without_dual_tasks()
        html = format_html_report(report)
        assert "Dual Verification Matrix" not in html
