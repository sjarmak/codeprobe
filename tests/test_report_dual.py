"""Report rendering tests for tasks carrying dual scoring_details."""

from __future__ import annotations

import csv
import io
import json

from codeprobe.analysis import (
    format_csv_report,
    format_html_report,
    format_json_report,
    format_text_report,
    generate_report,
    summarize_config,
)
from codeprobe.models.experiment import CompletedTask, ConfigResults


def _dual_task(
    task_id: str,
    *,
    direct: float,
    artifact: float,
    passed_direct: bool,
    passed_artifact: bool,
    cost: float | None = 0.05,
) -> CompletedTask:
    return CompletedTask(
        task_id=task_id,
        automated_score=min(direct, artifact),
        status="completed",
        duration_seconds=12.5,
        cost_usd=cost,
        scoring_details={
            "score_direct": direct,
            "score_artifact": artifact,
            "passed_direct": passed_direct,
            "passed_artifact": passed_artifact,
            "scoring_policy": "min",
        },
    )


def _single_task(task_id: str, score: float) -> CompletedTask:
    return CompletedTask(
        task_id=task_id,
        automated_score=score,
        status="completed",
        duration_seconds=10.0,
        cost_usd=0.03,
    )


def _make_report():
    dual = ConfigResults(
        config="dual-config",
        completed=[
            _dual_task(
                "t1",
                direct=0.9,
                artifact=0.8,
                passed_direct=True,
                passed_artifact=True,
            ),
            _dual_task(
                "t2",
                direct=0.6,
                artifact=0.3,
                passed_direct=True,
                passed_artifact=False,
            ),
            _dual_task(
                "t3",
                direct=0.2,
                artifact=0.1,
                passed_direct=False,
                passed_artifact=False,
            ),
        ],
    )
    single = ConfigResults(
        config="single-config",
        completed=[_single_task("t1", 1.0), _single_task("t2", 0.0)],
    )
    return generate_report("dual-exp", [dual, single])


# ---------------------------------------------------------------------------
# Summary stats
# ---------------------------------------------------------------------------


class TestSummaryDualStats:
    def test_dual_pass_rates_populated(self) -> None:
        dual = ConfigResults(
            config="dual",
            completed=[
                _dual_task(
                    "a",
                    direct=0.9,
                    artifact=0.8,
                    passed_direct=True,
                    passed_artifact=True,
                ),
                _dual_task(
                    "b",
                    direct=0.6,
                    artifact=0.3,
                    passed_direct=True,
                    passed_artifact=False,
                ),
            ],
        )
        s = summarize_config(dual)
        assert s.dual_task_count == 2
        assert s.direct_pass_rate == 1.0
        assert s.artifact_pass_rate == 0.5

    def test_single_config_has_none_dual_rates(self) -> None:
        single = ConfigResults(config="single", completed=[_single_task("a", 1.0)])
        s = summarize_config(single)
        assert s.dual_task_count == 0
        assert s.direct_pass_rate is None
        assert s.artifact_pass_rate is None


# ---------------------------------------------------------------------------
# Text report
# ---------------------------------------------------------------------------


class TestTextReportDual:
    def test_text_report_mentions_artifact(self) -> None:
        report = _make_report()
        text = format_text_report(report)
        # Artifact column appears in per-task table header.
        assert "Artifact" in text
        # Per-leg breakdown appears in rankings line for dual config.
        assert "code" in text and "artifact" in text

    def test_text_report_artifact_value_rendered(self) -> None:
        report = _make_report()
        text = format_text_report(report)
        # t1's artifact score is 0.80 — should appear formatted in the table.
        assert "0.80" in text


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------


class TestHtmlReportDual:
    def test_html_report_has_artifact_column(self) -> None:
        report = _make_report()
        html = format_html_report(report)
        assert "<th>Artifact</th>" in html

    def test_html_report_single_config_unchanged(self) -> None:
        # The single-config drill-down must NOT get an Artifact column.
        single = ConfigResults(config="single", completed=[_single_task("a", 1.0)])
        report = generate_report("only-single", [single])
        html = format_html_report(report)
        assert "<th>Artifact</th>" not in html


# ---------------------------------------------------------------------------
# CSV report
# ---------------------------------------------------------------------------


def _csv_reader_skipping_comments(text: str) -> csv.DictReader:
    """Skip leading '# ...' comment lines then parse CSV."""
    lines = [line for line in text.splitlines() if not line.startswith("#")]
    return csv.DictReader(io.StringIO("\n".join(lines)))


class TestCsvReportDual:
    def test_csv_includes_dual_columns(self) -> None:
        report = _make_report()
        text = format_csv_report(report)
        reader = _csv_reader_skipping_comments(text)
        assert "score_direct" in reader.fieldnames
        assert "score_artifact" in reader.fieldnames
        assert "passed_direct" in reader.fieldnames
        assert "passed_artifact" in reader.fieldnames
        assert "scoring_policy" in reader.fieldnames

    def test_csv_dual_row_values(self) -> None:
        report = _make_report()
        text = format_csv_report(report)
        reader = _csv_reader_skipping_comments(text)
        rows = list(reader)
        dual_rows = [r for r in rows if r["config"] == "dual-config"]
        assert len(dual_rows) == 3
        first = dual_rows[0]
        assert float(first["score_direct"]) == 0.9
        assert float(first["score_artifact"]) == 0.8
        assert first["passed_direct"] in ("True", "true", "1")
        assert first["scoring_policy"] == "min"

    def test_csv_single_row_leaves_dual_columns_blank(self) -> None:
        report = _make_report()
        text = format_csv_report(report)
        reader = _csv_reader_skipping_comments(text)
        rows = list(reader)
        single_rows = [r for r in rows if r["config"] == "single-config"]
        assert single_rows, "expected single-config rows"
        for r in single_rows:
            assert r["score_direct"] == ""
            assert r["score_artifact"] == ""
            assert r["passed_direct"] == ""
            assert r["passed_artifact"] == ""
            assert r["scoring_policy"] == ""


# ---------------------------------------------------------------------------
# JSON report
# ---------------------------------------------------------------------------


class TestJsonReportDual:
    def test_json_tasks_include_scoring_details(self) -> None:
        report = _make_report()
        data = json.loads(format_json_report(report))
        dual_tasks = [t for t in data["tasks"] if t["config"] == "dual-config"]
        assert len(dual_tasks) == 3
        sd = dual_tasks[0]["scoring_details"]
        assert sd["score_direct"] == 0.9
        assert sd["score_artifact"] == 0.8
        assert sd["passed_direct"] is True
        assert sd["passed_artifact"] is True
        assert sd["scoring_policy"] == "min"

    def test_json_summary_has_dual_rates(self) -> None:
        report = _make_report()
        data = json.loads(format_json_report(report))
        summaries = {s["label"]: s for s in data["summaries"]}
        assert summaries["dual-config"]["dual_task_count"] == 3
        assert summaries["dual-config"]["direct_pass_rate"] is not None
        assert summaries["dual-config"]["artifact_pass_rate"] is not None
        # single-config stays None
        assert summaries["single-config"]["dual_task_count"] == 0
        assert summaries["single-config"]["direct_pass_rate"] is None
        assert summaries["single-config"]["artifact_pass_rate"] is None

    def test_json_single_task_has_empty_scoring_details(self) -> None:
        report = _make_report()
        data = json.loads(format_json_report(report))
        single_tasks = [t for t in data["tasks"] if t["config"] == "single-config"]
        assert single_tasks
        for t in single_tasks:
            assert t["scoring_details"] == {}
