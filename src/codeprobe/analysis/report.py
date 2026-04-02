"""Report generation and formatting for experiment analysis."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass

from codeprobe.analysis.ranking import RankedConfig, rank_configs
from codeprobe.analysis.stats import (
    ConfigSummary,
    PairwiseComparison,
    compare_configs,
    summarize_completed_tasks,
    summarize_config,
)
from codeprobe.models.experiment import CompletedTask, ConfigResults


@dataclass(frozen=True)
class Report:
    """Complete analysis report."""

    experiment_name: str
    summaries: tuple[ConfigSummary, ...]
    rankings: tuple[RankedConfig, ...]
    comparisons: tuple[PairwiseComparison, ...]
    is_partial: bool = False
    tasks_expected: int | None = None
    completion_ratio: float | None = None
    config_results: tuple[ConfigResults, ...] = ()


def _compute_partial_metadata(
    summaries: list[ConfigSummary], total_tasks: int | None
) -> tuple[bool, int | None, float | None]:
    """Compute report-level partial metadata from summaries and total_tasks.

    Returns (is_partial, tasks_expected, completion_ratio).
    """
    if total_tasks is None:
        return False, None, None

    # Sum completed tasks across all configs (take max per config for ratio)
    tasks_completed = max((s.total_tasks for s in summaries), default=0)
    is_partial = tasks_completed < total_tasks
    completion_ratio = tasks_completed / total_tasks if total_tasks > 0 else 0.0
    return is_partial, total_tasks, completion_ratio


def generate_report(
    experiment_name: str,
    all_results: list[ConfigResults],
    *,
    total_tasks: int | None = None,
) -> Report:
    """Generate a full report from config results.

    When *total_tasks* is provided and exceeds completed tasks, the report
    is flagged as partial with a completion ratio.

    1. summarize_config() for each
    2. rank_configs()
    3. compare_configs() for all pairs
    4. Return Report
    """
    summaries = [summarize_config(r, total_tasks=total_tasks) for r in all_results]
    rankings = rank_configs(summaries)

    comparisons: list[PairwiseComparison] = []
    for i, a in enumerate(summaries):
        for b in summaries[i + 1 :]:
            comparisons.append(compare_configs(a, b))

    is_partial, tasks_expected, completion_ratio = _compute_partial_metadata(
        summaries, total_tasks
    )

    return Report(
        experiment_name=experiment_name,
        summaries=tuple(summaries),
        rankings=tuple(rankings),
        comparisons=tuple(comparisons),
        is_partial=is_partial,
        tasks_expected=tasks_expected,
        completion_ratio=completion_ratio,
        config_results=tuple(all_results),
    )


def generate_report_streaming(
    experiment_name: str,
    config_task_pairs: Iterator[tuple[str, Iterator[CompletedTask]]],
    *,
    total_tasks: int | None = None,
) -> Report:
    """Generate a report by streaming tasks per config.

    Each element of *config_task_pairs* is ``(config_label, tasks_iterator)``.
    Tasks are consumed in a single pass via summarize_completed_tasks(),
    avoiding loading all results into memory at once. Ranking and comparison
    operate on the resulting summaries (O(configs), not O(tasks)).

    When *total_tasks* is provided and exceeds completed tasks, the report
    is flagged as partial with a completion ratio.
    """
    summaries = [
        summarize_completed_tasks(label, tasks, total_tasks=total_tasks)
        for label, tasks in config_task_pairs
    ]
    rankings = rank_configs(summaries)

    comparisons: list[PairwiseComparison] = []
    for i, a in enumerate(summaries):
        for b in summaries[i + 1 :]:
            comparisons.append(compare_configs(a, b))

    is_partial, tasks_expected, completion_ratio = _compute_partial_metadata(
        summaries, total_tasks
    )

    return Report(
        experiment_name=experiment_name,
        summaries=tuple(summaries),
        rankings=tuple(rankings),
        comparisons=tuple(comparisons),
        is_partial=is_partial,
        tasks_expected=tasks_expected,
        completion_ratio=completion_ratio,
    )


def format_text_report(report: Report) -> str:
    """Format report as human-readable text."""
    lines: list[str] = []

    lines.append(f"## Experiment: {report.experiment_name}")
    lines.append("")

    if report.is_partial and report.tasks_expected is not None:
        tasks_done = max((s.total_tasks for s in report.summaries), default=0)
        pct = int((report.completion_ratio or 0.0) * 100)
        lines.append(f"**PARTIAL** {tasks_done}/{report.tasks_expected} tasks ({pct}%)")
        lines.append("")

    # Rankings
    lines.append("### Rankings")
    for rc in report.rankings:
        s = rc.summary
        cost_str = (
            f"${s.total_cost_usd:.2f} total"
            if s.total_cost_usd is not None
            else "no cost data"
        )
        lines.append(
            f"{rc.rank}. {rc.label} — {s.pass_rate:.0%} pass rate, "
            f"{cost_str} — {rc.recommendation}"
        )
    lines.append("")

    # Detailed Comparison
    if report.comparisons:
        lines.append("### Detailed Comparison")
        for c in report.comparisons:
            lines.append(c.summary)
        lines.append("")

    # Per-Task Results
    if report.config_results:
        lines.append("### Per-Task Results")
        lines.append("")
        lines.append("| Config | Task | Score | Pass | Duration (s) | Cost ($) |")
        lines.append("|--------|------|-------|------|--------------|----------|")
        for cr in report.config_results:
            for task in cr.completed:
                passed = "Y" if task.automated_score > 0 else "N"
                cost_cell = f"{task.cost_usd:.4f}" if task.cost_usd is not None else ""
                lines.append(
                    f"| {cr.config} | {task.task_id} | {task.automated_score:.2f} "
                    f"| {passed} | {task.duration_seconds:.1f} | {cost_cell} |"
                )
        lines.append("")

    # Recommendation
    lines.append("### Recommendation")
    if report.rankings:
        best = report.rankings[0]
        lines.append(f"Use {best.label} for best results.")

        cost_efficient = [
            r for r in report.rankings if "cost-efficiency" in r.recommendation.lower()
        ]
        if cost_efficient:
            lines.append(f"Consider {cost_efficient[0].label} if cost is a concern.")
    else:
        lines.append("No configurations to recommend.")

    return "\n".join(lines)


def _build_task_rows(report: Report) -> list[dict]:
    """Build per-task row dicts from report config_results and summaries."""
    summary_map = {s.label: s for s in report.summaries}
    rows: list[dict] = []
    for cr in report.config_results:
        summary = summary_map.get(cr.config)
        ci_lower = summary.ci_lower if summary else None
        ci_upper = summary.ci_upper if summary else None
        for task in cr.completed:
            rows.append(
                {
                    "config": cr.config,
                    "task_id": task.task_id,
                    "repeat": 1,
                    "score": task.automated_score,
                    "pass": 1 if task.automated_score > 0 else 0,
                    "duration_sec": task.duration_seconds,
                    "cost_usd": task.cost_usd,
                    "cost_source": task.cost_source,
                    "input_tokens": task.input_tokens,
                    "output_tokens": task.output_tokens,
                    "cache_read_tokens": task.cache_read_tokens,
                    "cost_model": task.cost_model,
                    "ci_lower": ci_lower,
                    "ci_upper": ci_upper,
                }
            )
    return rows


def format_json_report(report: Report) -> str:
    """Format report as JSON string."""
    data: dict = {
        "experiment_name": report.experiment_name,
        "is_partial": report.is_partial,
        "tasks_expected": report.tasks_expected,
        "completion_ratio": report.completion_ratio,
        "summaries": [asdict(s) for s in report.summaries],
        "rankings": [
            {
                "rank": r.rank,
                "label": r.label,
                "recommendation": r.recommendation,
                "summary": asdict(r.summary),
            }
            for r in report.rankings
        ],
        "comparisons": [asdict(c) for c in report.comparisons],
        "tasks": _build_task_rows(report),
    }
    return json.dumps(data, indent=2)


_CSV_COLUMNS = [
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
    "cost_model",
    "ci_lower",
    "ci_upper",
]


def format_csv_report(report: Report) -> str:
    """Format report as CSV with per-task rows."""
    buf = io.StringIO()

    has_warning = any(s.sample_size_warning for s in report.summaries)
    if has_warning:
        buf.write("# SINGLE RUN — no statistical confidence\n")

    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS)
    writer.writeheader()
    for row in _build_task_rows(report):
        writer.writerow(row)

    return buf.getvalue()
