"""Report generation and formatting for experiment analysis."""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass

from codeprobe.analysis.dual import has_dual_scoring
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

    # Are any dual-scored tasks present anywhere in the report? Used to
    # decide whether to expand the per-task table with an Artifact column
    # and to annotate the rankings line with per-leg pass rates.
    any_dual_tasks = any((s.dual_task_count or 0) > 0 for s in report.summaries)

    # Rankings
    lines.append("### Rankings")
    for rc in report.rankings:
        s = rc.summary
        cost_str = (
            f"${s.total_cost_usd:.2f} total"
            if s.total_cost_usd is not None
            else "no cost data"
        )
        dual_suffix = ""
        if s.direct_pass_rate is not None and s.artifact_pass_rate is not None:
            dual_suffix = (
                f" (code {s.direct_pass_rate:.0%} / "
                f"artifact {s.artifact_pass_rate:.0%})"
            )
        lines.append(
            f"{rc.rank}. {rc.label} — {s.pass_rate:.0%} pass rate{dual_suffix}, "
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
        if any_dual_tasks:
            lines.append(
                "| Config | Task | Score | Artifact | Pass | Duration (s) | Cost ($) |"
            )
            lines.append(
                "|--------|------|-------|----------|------|--------------|----------|"
            )
        else:
            lines.append("| Config | Task | Score | Pass | Duration (s) | Cost ($) |")
            lines.append("|--------|------|-------|------|--------------|----------|")
        for cr in report.config_results:
            for task in cr.completed:
                passed = "Y" if task.automated_score > 0 else "N"
                cost_cell = f"{task.cost_usd:.4f}" if task.cost_usd is not None else ""
                if any_dual_tasks:
                    details = task.scoring_details or {}
                    if "score_artifact" in details:
                        artifact_cell = f"{float(details['score_artifact']):.2f}"
                    else:
                        artifact_cell = "—"
                    lines.append(
                        f"| {cr.config} | {task.task_id} "
                        f"| {task.automated_score:.2f} | {artifact_cell} "
                        f"| {passed} | {task.duration_seconds:.1f} | {cost_cell} |"
                    )
                else:
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
            details = task.scoring_details or {}
            has_dual = has_dual_scoring(task)
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
                    # Dual scoring leg columns — populated when the task has
                    # dual scoring_details, otherwise None/empty so CSV
                    # still emits a uniform schema.
                    "score_direct": (details.get("score_direct") if has_dual else None),
                    "score_artifact": (
                        details.get("score_artifact") if has_dual else None
                    ),
                    "passed_direct": (
                        details.get("passed_direct") if has_dual else None
                    ),
                    "passed_artifact": (
                        details.get("passed_artifact") if has_dual else None
                    ),
                    "scoring_policy": (
                        details.get("scoring_policy", "") if has_dual else ""
                    ),
                    # Full scoring_details dict — JSON export preserves this
                    # verbatim; CSV writer ignores it via extrasaction='ignore'.
                    "scoring_details": dict(details),
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
    # Dual scoring legs — always present in the CSV schema so consumers can
    # rely on a stable column set. Empty strings for non-dual tasks.
    "score_direct",
    "score_artifact",
    "passed_direct",
    "passed_artifact",
    "scoring_policy",
]


def format_html_report(report: Report) -> str:
    """Format report as a self-contained HTML file with inline CSS/JS."""
    parts: list[str] = []

    best_label = report.rankings[0].label if report.rankings else "N/A"
    best_rec = report.rankings[0].recommendation if report.rankings else ""
    has_single_run = any(s.sample_size_warning for s in report.summaries)

    # --- Helpers ---
    def _esc(text: str) -> str:
        """Minimal HTML escaping."""
        return (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def _fmt_cost(cost: float | None) -> str:
        return f"${cost:.4f}" if cost is not None else "—"

    def _fmt_pct(val: float) -> str:
        return f"{val:.0%}"

    def _fmt_score(val: float) -> str:
        return f"{val:.2f}"

    def _ci_bar_html(s: ConfigSummary) -> str:
        """Render CI bar or single-run banner for a summary."""
        if s.sample_size_warning:
            return '<span class="single-run-badge">Single run</span>'
        lo = s.ci_lower * 100
        hi = s.ci_upper * 100
        mid = s.pass_rate * 100
        return (
            f'<div class="ci-bar">'
            f'<div class="ci-range" style="left:{lo:.1f}%;width:{hi - lo:.1f}%"></div>'
            f'<div class="ci-point" style="left:{mid:.1f}%"></div>'
            f"</div>"
        )

    # --- HTML start ---
    parts.append("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>""")
    parts.append(_esc(report.experiment_name))
    parts.append(""" — codeprobe report</title>
<style>
:root{--bg:#f8f9fa;--card:#fff;--border:#dee2e6;--text:#212529;--muted:#6c757d;
--accent:#0d6efd;--success:#198754;--warning:#ffc107;--danger:#dc3545}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--text);line-height:1.6;padding:2rem;max-width:1200px;margin:0 auto}
h1{font-size:1.8rem;margin-bottom:.5rem}
h2{font-size:1.3rem;margin:2rem 0 1rem;border-bottom:2px solid var(--accent);padding-bottom:.3rem}
h3{font-size:1.1rem;margin:1.5rem 0 .5rem}
.subtitle{color:var(--muted);margin-bottom:1.5rem}
.card{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:1.2rem;margin-bottom:1rem}
.executive{border-left:4px solid var(--accent)}
.single-run-banner{background:var(--warning);color:#000;padding:.5rem 1rem;border-radius:4px;margin-bottom:1rem;font-weight:600}
.single-run-badge{background:var(--warning);color:#000;padding:2px 8px;border-radius:4px;font-size:.8rem;font-weight:600}
table{width:100%;border-collapse:collapse;margin:.5rem 0}
th,td{padding:.5rem .75rem;text-align:left;border-bottom:1px solid var(--border)}
th{background:#e9ecef;font-weight:600;font-size:.85rem;text-transform:uppercase;letter-spacing:.03em}
tr:hover{background:#f1f3f5}
.pass{color:var(--success);font-weight:600}
.fail{color:var(--danger);font-weight:600}
.winner-badge{background:var(--success);color:#fff;padding:2px 8px;border-radius:4px;font-size:.8rem}
.pairwise-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(350px,1fr));gap:1rem}
.pairwise-card{border:1px solid var(--border);border-radius:8px;padding:1rem;background:var(--card)}
.pairwise-card h4{margin-bottom:.5rem}
.stat-row{display:flex;justify-content:space-between;padding:.25rem 0;border-bottom:1px solid #f0f0f0}
.stat-label{color:var(--muted);font-size:.85rem}
.stat-value{font-weight:600}
.cost-section{display:grid;grid-template-columns:1fr 1fr;gap:1rem}
.cost-group h3{margin-top:0}
.ci-bar{position:relative;height:8px;background:#e9ecef;border-radius:4px;margin:.3rem 0}
.ci-range{position:absolute;height:100%;background:rgba(13,110,253,.25);border-radius:4px}
.ci-point{position:absolute;width:3px;height:100%;background:var(--accent);border-radius:2px;transform:translateX(-50%)}
details{margin:.5rem 0}
summary{cursor:pointer;font-weight:600;padding:.4rem 0}
.partial-banner{background:#fff3cd;border:1px solid #ffecb5;padding:.5rem 1rem;border-radius:4px;margin-bottom:1rem}
</style>
</head>
<body>
""")

    # --- Header ---
    parts.append(f"<h1>{_esc(report.experiment_name)}</h1>\n")
    parts.append('<p class="subtitle">Generated by codeprobe</p>\n')

    if report.is_partial and report.tasks_expected is not None:
        tasks_done = max((s.total_tasks for s in report.summaries), default=0)
        pct = int((report.completion_ratio or 0.0) * 100)
        parts.append(
            f'<div class="partial-banner">PARTIAL — {tasks_done}/{report.tasks_expected} '
            f"tasks ({pct}%) completed</div>\n"
        )

    if has_single_run:
        parts.append(
            '<div class="single-run-banner">'
            "Single run — no confidence intervals available</div>\n"
        )

    # --- Executive Summary ---
    parts.append('<h2 id="executive-summary">Executive Summary</h2>\n')
    parts.append('<div class="card executive">\n')
    if report.rankings:
        best_s = report.rankings[0].summary
        cost_str = _fmt_cost(best_s.total_cost_usd)
        parts.append(
            f"<p><strong>Recommendation:</strong> {_esc(best_label)} — "
            f"{_esc(best_rec)}</p>\n"
        )
        parts.append(
            f"<p>Pass rate: {_fmt_pct(best_s.pass_rate)} | "
            f"Mean score: {_fmt_score(best_s.mean_score)} | "
            f"Cost: {cost_str}</p>\n"
        )
    else:
        parts.append("<p>No configurations to recommend.</p>\n")
    parts.append("</div>\n")

    # --- Ranking Table ---
    parts.append('<h2 id="ranking-table">Rankings</h2>\n')
    parts.append("<table>\n<thead><tr>")
    parts.append(
        "<th>Rank</th><th>Config</th><th>Pass Rate</th>"
        "<th>Mean Score</th><th>Cost</th><th>Billing</th><th>CI</th>"
    )
    parts.append("</tr></thead>\n<tbody>\n")
    for rc in report.rankings:
        s = rc.summary
        parts.append(
            f"<tr><td>{rc.rank}</td><td>{_esc(rc.label)}</td>"
            f"<td>{_fmt_pct(s.pass_rate)}</td>"
            f"<td>{_fmt_score(s.mean_score)}</td>"
            f"<td>{_fmt_cost(s.total_cost_usd)}</td>"
            f"<td>{_esc(s.billing_model)}</td>"
            f"<td>{_ci_bar_html(s)}</td></tr>\n"
        )
    parts.append("</tbody>\n</table>\n")

    # --- Per-Task Drill-Down ---
    if report.config_results:
        parts.append('<h2 id="per-task-drilldown">Per-Task Drill-Down</h2>\n')
        for cr in report.config_results:
            # Expand the table with an Artifact column when any task in this
            # config has dual scoring details.
            config_has_dual = any(has_dual_scoring(task) for task in cr.completed)
            parts.append(f"<details>\n<summary>{_esc(cr.config)}</summary>\n")
            parts.append("<table>\n<thead><tr>")
            if config_has_dual:
                parts.append(
                    "<th>Task</th><th>Score</th><th>Artifact</th><th>Pass</th>"
                    "<th>Duration (s)</th><th>Cost</th>"
                )
            else:
                parts.append(
                    "<th>Task</th><th>Score</th><th>Pass</th>"
                    "<th>Duration (s)</th><th>Cost</th>"
                )
            parts.append("</tr></thead>\n<tbody>\n")
            for task in cr.completed:
                passed = task.automated_score > 0
                cls = "pass" if passed else "fail"
                if config_has_dual:
                    details = task.scoring_details or {}
                    if "score_artifact" in details:
                        artifact_cell = _fmt_score(float(details["score_artifact"]))
                    else:
                        artifact_cell = "—"
                    parts.append(
                        f"<tr><td>{_esc(task.task_id)}</td>"
                        f"<td>{_fmt_score(task.automated_score)}</td>"
                        f"<td>{artifact_cell}</td>"
                        f'<td class="{cls}">{"Y" if passed else "N"}</td>'
                        f"<td>{task.duration_seconds:.1f}</td>"
                        f"<td>{_fmt_cost(task.cost_usd)}</td></tr>\n"
                    )
                else:
                    parts.append(
                        f"<tr><td>{_esc(task.task_id)}</td>"
                        f"<td>{_fmt_score(task.automated_score)}</td>"
                        f'<td class="{cls}">{"Y" if passed else "N"}</td>'
                        f"<td>{task.duration_seconds:.1f}</td>"
                        f"<td>{_fmt_cost(task.cost_usd)}</td></tr>\n"
                    )
            parts.append("</tbody>\n</table>\n</details>\n")

    # --- Pairwise Comparison Cards ---
    if report.comparisons:
        parts.append('<h2 id="pairwise-comparisons">Pairwise Comparisons</h2>\n')
        parts.append('<div class="pairwise-grid">\n')
        for c in report.comparisons:
            parts.append('<div class="pairwise-card">\n')
            parts.append(
                f"<h4>{_esc(c.config_a)} vs {_esc(c.config_b)} "
                f'<span class="winner-badge">Winner: {_esc(c.winner)}</span></h4>\n'
            )
            parts.append(
                f'<div class="stat-row"><span class="stat-label">Score diff</span>'
                f'<span class="stat-value">{c.score_diff:+.3f}</span></div>\n'
            )
            if c.effect_size is not None:
                parts.append(
                    f'<div class="stat-row"><span class="stat-label">'
                    f"Effect size ({_esc(c.effect_size_method)})</span>"
                    f'<span class="stat-value">{c.effect_size:.3f}</span></div>\n'
                )
            if c.p_value is not None:
                parts.append(
                    f'<div class="stat-row"><span class="stat-label">p-value</span>'
                    f'<span class="stat-value">{c.p_value:.4f}</span></div>\n'
                )
            if not has_single_run:
                parts.append(
                    f'<div class="stat-row"><span class="stat-label">CI</span>'
                    f'<span class="stat-value">[{c.ci_lower:.3f}, {c.ci_upper:.3f}]</span></div>\n'
                )
            parts.append("</div>\n")
        parts.append("</div>\n")

    # --- Cost Efficiency Section ---
    parts.append('<h2 id="cost-efficiency">Cost Efficiency</h2>\n')
    per_token = [s for s in report.summaries if s.billing_model in ("api", "per-token")]
    subscription = [
        s for s in report.summaries if s.billing_model in ("session", "subscription")
    ]
    other = [
        s for s in report.summaries if s not in per_token and s not in subscription
    ]

    def _cost_table(summaries: list[ConfigSummary]) -> str:
        if not summaries:
            return "<p>None</p>\n"
        rows: list[str] = []
        rows.append(
            "<table>\n<thead><tr><th>Config</th><th>Total Cost</th>"
            "<th>Cost/Task</th><th>Pass Rate</th></tr></thead>\n<tbody>\n"
        )
        for s in summaries:
            cost_per_task = (
                f"${s.total_cost_usd / s.total_tasks:.4f}"
                if s.total_cost_usd is not None and s.total_tasks > 0
                else "—"
            )
            rows.append(
                f"<tr><td>{_esc(s.label)}</td>"
                f"<td>{_fmt_cost(s.total_cost_usd)}</td>"
                f"<td>{cost_per_task}</td>"
                f"<td>{_fmt_pct(s.pass_rate)}</td></tr>\n"
            )
        rows.append("</tbody>\n</table>\n")
        return "".join(rows)

    parts.append('<div class="cost-section">\n')
    parts.append('<div class="cost-group">\n')
    parts.append("<h3>Per-Token Billing</h3>\n")
    parts.append(_cost_table(per_token))
    parts.append("</div>\n")
    parts.append('<div class="cost-group">\n')
    parts.append("<h3>Subscription Billing</h3>\n")
    parts.append(_cost_table(subscription))
    parts.append("</div>\n")
    parts.append("</div>\n")

    if other:
        parts.append("<h3>Other / Unknown</h3>\n")
        parts.append(_cost_table(other))

    # --- Footer ---
    parts.append("""
<script>
// Toggle all details sections
document.querySelectorAll('details summary').forEach(s=>{
  s.addEventListener('click',e=>{
    if(e.altKey){
      const open=!s.parentElement.open;
      document.querySelectorAll('details').forEach(d=>{d.open=open});
      e.preventDefault();
    }
  });
});
</script>
</body>
</html>""")

    return "".join(parts)


def format_csv_report(report: Report) -> str:
    """Format report as CSV with per-task rows."""
    buf = io.StringIO()

    has_warning = any(s.sample_size_warning for s in report.summaries)
    if has_warning:
        buf.write("# SINGLE RUN — no statistical confidence\n")

    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in _build_task_rows(report):
        # Replace None with empty string for optional dual columns so the CSV
        # shows a blank cell rather than the string "None".
        csv_row = {k: ("" if row.get(k) is None else row.get(k)) for k in _CSV_COLUMNS}
        writer.writerow(csv_row)

    return buf.getvalue()
