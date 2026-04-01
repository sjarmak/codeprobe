"""Report generation and formatting for experiment analysis."""

from __future__ import annotations

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


def generate_report(experiment_name: str, all_results: list[ConfigResults]) -> Report:
    """Generate a full report from config results.

    1. summarize_config() for each
    2. rank_configs()
    3. compare_configs() for all pairs
    4. Return Report
    """
    summaries = [summarize_config(r) for r in all_results]
    rankings = rank_configs(summaries)

    comparisons: list[PairwiseComparison] = []
    for i, a in enumerate(summaries):
        for b in summaries[i + 1 :]:
            comparisons.append(compare_configs(a, b))

    return Report(
        experiment_name=experiment_name,
        summaries=tuple(summaries),
        rankings=tuple(rankings),
        comparisons=tuple(comparisons),
    )


def generate_report_streaming(
    experiment_name: str,
    config_task_pairs: Iterator[tuple[str, Iterator[CompletedTask]]],
) -> Report:
    """Generate a report by streaming tasks per config.

    Each element of *config_task_pairs* is ``(config_label, tasks_iterator)``.
    Tasks are consumed in a single pass via summarize_completed_tasks(),
    avoiding loading all results into memory at once. Ranking and comparison
    operate on the resulting summaries (O(configs), not O(tasks)).
    """
    summaries = [
        summarize_completed_tasks(label, tasks) for label, tasks in config_task_pairs
    ]
    rankings = rank_configs(summaries)

    comparisons: list[PairwiseComparison] = []
    for i, a in enumerate(summaries):
        for b in summaries[i + 1 :]:
            comparisons.append(compare_configs(a, b))

    return Report(
        experiment_name=experiment_name,
        summaries=tuple(summaries),
        rankings=tuple(rankings),
        comparisons=tuple(comparisons),
    )


def format_text_report(report: Report) -> str:
    """Format report as human-readable text."""
    lines: list[str] = []

    lines.append(f"## Experiment: {report.experiment_name}")
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


def format_json_report(report: Report) -> str:
    """Format report as JSON string."""
    data = {
        "experiment_name": report.experiment_name,
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
    }
    return json.dumps(data, indent=2)
