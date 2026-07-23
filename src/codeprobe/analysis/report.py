"""Report generation and formatting for experiment analysis."""

from __future__ import annotations

import csv
import io
import json
import statistics
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, replace

from codeprobe.analysis.dual import dual_matrix, has_dual_scoring
from codeprobe.analysis.ranking import RankedConfig, rank_configs
from codeprobe.analysis.stats import (
    ConfigSummary,
    PairwiseComparison,
    _comparison_summary,
    _derive_verdict,
    compare_configs,
    holm_adjusted,
    is_scorable_run,
    summarize_completed_tasks,
    summarize_config,
    task_passed,
)
from codeprobe.analysis.validity import ValidityReport, ValidityTriage, triage_run
from codeprobe.models.experiment import (
    CompletedTask,
    ConfigResults,
    ExperimentConfig,
)


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
    # Infra-failure validity triage over every trial in the report. ``passed``
    # is False whenever an unresolved infra casualty remains — the run is NOT
    # quotable until those trials are re-run (codeprobe-77z). None only for
    # reports built without trial-level data.
    validity: ValidityReport | None = None


def _compute_partial_metadata(
    summaries: list[ConfigSummary], total_tasks: int | None
) -> tuple[bool, int | None, float | None]:
    """Compute report-level partial metadata from summaries and total_tasks.

    Returns (is_partial, tasks_expected, completion_ratio). Worst-arm
    semantics: the report is partial when ANY arm is partial — one complete
    arm must never mask a crashed one — and ``completion_ratio`` is the
    worst arm's distinct-task coverage, not the best arm's
    (codeprobe-f7rl.9; locked decision 6, epic codeprobe-f7rl).
    """
    if total_tasks is None:
        return False, None, None

    is_partial = any(s.is_partial for s in summaries)
    if total_tasks > 0:
        worst_distinct = min((s.distinct_task_count for s in summaries), default=0)
        completion_ratio = worst_distinct / total_tasks
    else:
        completion_ratio = 0.0
    return is_partial, total_tasks, completion_ratio


def generate_report(
    experiment_name: str,
    all_results: list[ConfigResults],
    *,
    total_tasks: int | None = None,
    configs: list[ExperimentConfig] | None = None,
) -> Report:
    """Generate a full report from config results.

    When *total_tasks* is provided and exceeds completed tasks, the report
    is flagged as partial with a completion ratio. When *configs* is
    provided, each summary's ``abandoned_surface_count`` is populated by the
    tool-surface audit (codeprobe-1gg): configs are matched to results by
    label so an arm that declared a surface but never used it is flagged.

    1. summarize_config() for each
    2. rank_configs()
    3. compare_configs() for all pairs
    4. Return Report
    """
    config_by_label = {c.label: c for c in (configs or [])}
    summaries = [
        summarize_config(
            r, total_tasks=total_tasks, config=config_by_label.get(r.config)
        )
        for r in all_results
    ]
    rankings = rank_configs(summaries)

    # Build per-config raw scores keyed by task_id. compare_configs
    # auto-detects binary vs continuous via _is_binary and picks the right
    # statistical test (McNemar + Cliff's delta for binary, Wilcoxon +
    # Cohen's d for continuous).
    config_scores: dict[str, dict[str, list[float]]] = {}
    for cr in all_results:
        # Restrict to scorable runs so the paired hypothesis tests and effect
        # sizes in compare_configs match the reward population the summaries
        # report — non-executed runs (quota, invalid-model, crash) are excluded
        # (codeprobe-a8r; broadened to all status=="error" in codeprobe-h3j4).
        # Every scorable repeat is accumulated per task_id so repeat trials
        # don't overwrite each other (codeprobe-f7rl.7).
        per_task: dict[str, list[float]] = {}
        for t in cr.completed:
            if is_scorable_run(t):
                per_task.setdefault(t.task_id, []).append(float(t.automated_score))
        config_scores[cr.config] = per_task

    comparisons: list[PairwiseComparison] = []
    for i, a in enumerate(summaries):
        for b in summaries[i + 1 :]:
            a_scores, b_scores = _paired_task_scores(config_scores, a.label, b.label)
            comparisons.append(
                compare_configs(a, b, a_scores=a_scores, b_scores=b_scores)
            )

    # k>2 runs a family of tests, so gate "wins" on Holm-adjusted p-values
    # (codeprobe-f7rl.10). k=2 is a single test: no correction.
    if len(summaries) > 2:
        comparisons = _apply_multiple_comparison_correction(comparisons)

    is_partial, tasks_expected, completion_ratio = _compute_partial_metadata(
        summaries, total_tasks
    )

    # Infra-failure validity gate over every trial across every config: a run
    # with an unresolved infra casualty is not quotable (codeprobe-77z).
    validity = triage_run(t for cr in all_results for t in cr.completed)

    return Report(
        experiment_name=experiment_name,
        summaries=tuple(summaries),
        rankings=tuple(rankings),
        comparisons=tuple(comparisons),
        is_partial=is_partial,
        tasks_expected=tasks_expected,
        completion_ratio=completion_ratio,
        config_results=tuple(all_results),
        validity=validity,
    )


def _tee_task_scores(
    tasks: Iterator[CompletedTask],
    sink: dict[str, list[float]],
    triage: ValidityTriage | None = None,
) -> Iterator[CompletedTask]:
    """Yield tasks unchanged while recording real trials' raw scores into *sink*.

    Stores ``automated_score`` (continuous) rather than a binarized pass/fail
    indicator so pairwise statistical tests can operate on the true score
    distribution and choose Wilcoxon + Cohen's d for continuous scorers
    vs McNemar + Cliff's delta for binary ones. Every scorable repeat is
    appended to the task's score list so repeat trials don't overwrite each
    other (codeprobe-f7rl.7). Non-executed runs and infra casualties are
    yielded but omitted from *sink* so the paired tests match the reward
    population (codeprobe-a8r; codeprobe-h3j4; codeprobe-77z). When *triage*
    is supplied every trial is also fed to it, so the streaming path gets the
    same validity gate as the batch one without buffering the trials.
    """
    for t in tasks:
        if triage is not None:
            triage.observe(t)
        # Excluded runs are still yielded (so the summarizer counts them in
        # quota_error_count / infra_failure_count / errored_count) but kept out
        # of the paired-score sink so compare_configs's statistical tests match
        # the reward population (codeprobe-a8r; codeprobe-h3j4; codeprobe-77z).
        if is_scorable_run(t):
            sink.setdefault(t.task_id, []).append(float(t.automated_score))
        yield t


def _paired_task_scores(
    config_scores: dict[str, dict[str, list[float]]],
    label_a: str,
    label_b: str,
) -> tuple[list[float] | None, list[float] | None]:
    """Extract paired per-task score lists for two configs.

    Returns ``(a_scores, b_scores)`` containing only tasks present in both
    configs (paired by task_id), or ``(None, None)`` when there are no
    shared tasks. Each emitted value is the mean over that task's scorable
    repeats — the per-task mean is the statistical unit for ``--repeats``
    (locked decision 6, epic codeprobe-f7rl). Means of binary repeats become
    continuous, so compare_configs' _is_binary auto-routes to Wilcoxon +
    Cohen's d, which is correct for aggregated units. When repeats are
    unbalanced (some repeats excluded as casualties) the mean is over the
    scorable repeats available.
    """
    a_by_id = config_scores.get(label_a, {})
    b_by_id = config_scores.get(label_b, {})
    shared_ids = sorted(set(a_by_id) & set(b_by_id))
    if not shared_ids:
        return None, None
    return (
        [statistics.mean(a_by_id[tid]) for tid in shared_ids],
        [statistics.mean(b_by_id[tid]) for tid in shared_ids],
    )


def _apply_multiple_comparison_correction(
    comparisons: list[PairwiseComparison],
) -> list[PairwiseComparison]:
    """Holm-correct the family of pairwise tests for a k>2 experiment.

    Runs all-at-once over the C(k,2) comparisons: REFUSED pairs contribute
    ``None`` to the family and are never re-verdicted; every comparable pair
    gets ``p_value_adjusted``, ``correction="holm"``, ``n_comparisons=m``
    (the number of tested pairs), and its verdict/summary re-derived from
    the ADJUSTED p — so "wins" is gated on the corrected result (locked
    decision 6, epic codeprobe-f7rl). Callers gate on k>2; k=2 reports are
    untouched by construction.
    """
    raw = [c.p_value if c.comparable else None for c in comparisons]
    m = sum(1 for p in raw if p is not None)
    if m == 0:
        return comparisons
    adjusted = holm_adjusted(raw)

    corrected: list[PairwiseComparison] = []
    for c, adj_p in zip(comparisons, adjusted):
        if not c.comparable:
            corrected.append(c)
            continue
        verdict = _derive_verdict(
            c.winner, c.score_diff, c.effect_size, c.effect_size_method, adj_p
        )
        summary = _comparison_summary(
            c.config_a, c.config_b, c.score_diff, c.cost_diff, c.speed_diff, verdict
        )
        corrected.append(
            replace(
                c,
                summary=summary,
                verdict=verdict,
                p_value_adjusted=adj_p,
                correction="holm",
                n_comparisons=m,
            )
        )
    return corrected


def _holm_disclosure(report: Report) -> str | None:
    """Disclosure sentence for Holm-corrected reports, or None for k=2."""
    holm = [c for c in report.comparisons if c.correction == "holm"]
    if not holm:
        return None
    return (
        f"{len(report.summaries)} arms -> {holm[0].n_comparisons} pairwise "
        "tests; p-values Holm-corrected (family-wise alpha=0.05)"
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
    config_scores: dict[str, dict[str, list[float]]] = {}
    summaries: list[ConfigSummary] = []
    # The gate runs over the streamed trials of every config (codeprobe-77z) —
    # accumulated in the tee so no trial has to be buffered.
    triage = ValidityTriage()
    for label, tasks in config_task_pairs:
        sink: dict[str, list[float]] = {}
        config_scores[label] = sink
        summaries.append(
            summarize_completed_tasks(
                label, _tee_task_scores(tasks, sink, triage), total_tasks=total_tasks
            )
        )

    rankings = rank_configs(summaries)

    comparisons: list[PairwiseComparison] = []
    for i, a in enumerate(summaries):
        for b in summaries[i + 1 :]:
            a_scores, b_scores = _paired_task_scores(config_scores, a.label, b.label)
            comparisons.append(
                compare_configs(a, b, a_scores=a_scores, b_scores=b_scores)
            )

    # Same k>2 Holm gate as generate_report — streaming parity
    # (codeprobe-f7rl.10).
    if len(summaries) > 2:
        comparisons = _apply_multiple_comparison_correction(comparisons)

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
        validity=triage.report(),
    )


def _worst_arm_partial_line(report: Report) -> str:
    """Worst-arm PARTIAL disclosure shared by the text and HTML surfaces.

    Names the least-complete arm and its distinct-task coverage — the best
    arm's count must never headline a partial report (codeprobe-f7rl.9).
    """
    worst = min(
        report.summaries, key=lambda s: s.distinct_task_count, default=None
    )
    worst_label = worst.label if worst is not None else "(no arms)"
    worst_n = worst.distinct_task_count if worst is not None else 0
    pct = int((report.completion_ratio or 0.0) * 100)
    return (
        f"PARTIAL — worst arm {worst_label}: "
        f"{worst_n}/{report.tasks_expected} tasks ({pct}%)"
    )


def _cost_covered_count(s: ConfigSummary) -> int:
    """Number of scorable trials with captured cost, recovered from coverage.

    ``cost_coverage`` is covered/scored exactly, so rounding the product
    recovers the integer numerator without a separate stored field
    (codeprobe-f7rl.35).
    """
    return round(s.cost_coverage * s.scored_count)


def _cost_source_breakdown(s: ConfigSummary) -> str:
    """Provenance summary: the single source name, or 'a 8, b 2' when mixed."""
    sources = {
        name: count
        for name, count in s.cost_source_counts.items()
        if name != "unavailable" and count > 0
    }
    if not sources:
        return "unknown source"
    if len(sources) == 1:
        return next(iter(sources))
    return ", ".join(
        f"{name} {count}"
        for name, count in sorted(sources.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def _dominant_cost_source(s: ConfigSummary) -> str:
    """Most common non-'unavailable' cost source, or '' when none."""
    sources = [
        (name, count)
        for name, count in s.cost_source_counts.items()
        if name != "unavailable" and count > 0
    ]
    if not sources:
        return ""
    return sorted(sources, key=lambda kv: (-kv[1], kv[0]))[0][0]


def _ranking_cost_str(s: ConfigSummary) -> str:
    """Per-arm cost phrase with coverage and provenance (codeprobe-f7rl.35).

    Full coverage: '$1.00 total (10/10 trials, api_reported)'. Partial
    coverage is an undercount, so the phrase carries an explicit
    not-comparable flag instead of masquerading as a total.
    """
    if s.total_cost_usd is None:
        return "no cost data"
    covered = _cost_covered_count(s)
    if s.cost_coverage == 1.0:
        return (
            f"${s.total_cost_usd:.2f} total "
            f"({covered}/{s.scored_count} trials, {_cost_source_breakdown(s)})"
        )
    return (
        f"${s.total_cost_usd:.2f} on {covered}/{s.scored_count} trials "
        f"— not comparable"
    )


def _cost_provenance_note(summaries: Iterable[ConfigSummary]) -> str | None:
    """Report-level cost disclosure, or None when costs are comparable.

    Fires when any rendered cost number is decision-unsafe: some arm's
    coverage is below 1.0, or arms disagree on dominant provenance. Returns
    None when no arm has cost data at all — there is no misleading number to
    flag (codeprobe-f7rl.35).
    """
    all_summaries = list(summaries)
    with_cost = [s for s in all_summaries if s.total_cost_usd is not None]
    if not with_cost:
        return None
    partial = any(s.cost_coverage < 1.0 for s in all_summaries)
    dominants = {d for s in with_cost if (d := _dominant_cost_source(s))}
    mixed_provenance = len(dominants) > 1
    if not partial and not mixed_provenance:
        return None
    reasons: list[str] = []
    if partial:
        reasons.append("cost was not captured on every trial of every arm")
    if mixed_provenance:
        reasons.append(
            "arms differ in dominant cost provenance "
            f"({', '.join(sorted(dominants))})"
        )
    return (
        "**Cost note:** "
        + "; ".join(reasons)
        + ". Cost totals are shown with per-arm coverage/provenance but were "
        "EXCLUDED from winner tiebreaks and 'Best cost-efficiency' "
        "recommendations — partial or mixed-provenance costs must not drive "
        "decisions (codeprobe-f7rl.35)."
    )


def format_text_report(report: Report) -> str:
    """Format report as human-readable text."""
    lines: list[str] = []

    lines.append(f"## Experiment: {report.experiment_name}")
    lines.append("")

    if report.is_partial and report.tasks_expected is not None:
        lines.append(f"**{_worst_arm_partial_line(report)}**")
        lines.append("")

    # Are any dual-scored tasks present anywhere in the report? Used to
    # decide whether to expand the per-task table with an Artifact column
    # and to annotate the rankings line with per-leg pass rates.
    any_dual_tasks = any((s.dual_task_count or 0) > 0 for s in report.summaries)

    # Rankings. Use mean_score as the headline metric for continuous
    # scorers (F1, partial credit) since pass_rate collapses the signal —
    # tasks with scores like 0.08 still count as "passed" when the scorer
    # emits ``passed: true``. For truly binary scorers pass_rate IS the
    # signal and is shown as-is.
    lines.append("### Rankings")
    for rc in report.rankings:
        s = rc.summary
        # codeprobe-f7rl.35: cost with coverage + provenance; partial coverage
        # renders an explicit not-comparable flag instead of a bare total.
        cost_str = _ranking_cost_str(s)
        dual_suffix = ""
        if s.direct_pass_rate is not None and s.artifact_pass_rate is not None:
            dual_suffix = (
                f" (code {s.direct_pass_rate:.0%} / "
                f"artifact {s.artifact_pass_rate:.0%})"
            )
        # codeprobe-h3j4: a config with no scorable run never executed; show
        # ERRORED (n) instead of a vacuous 0.00 mean / 0% pass row.
        if s.scored_count == 0:
            headline = f"ERRORED ({s.errored_count}) — no runs executed"
        elif s.score_type == "continuous":
            headline = (
                f"mean={s.mean_score:.2f} "
                f"[CI {s.ci_lower:.2f}–{s.ci_upper:.2f}]"
            )
        else:
            headline = f"{s.pass_rate:.0%} pass rate"
        # codeprobe-9xrl: when any trials were lost to OAuth quota,
        # surface the count beside the headline so readers don't
        # interpret the 0-scored quota errors as task-quality failures.
        quota_suffix = ""
        if s.quota_error_count > 0:
            quota_suffix = f" ⚠ {s.quota_error_count} quota error(s)"
        # codeprobe-77z: infra casualties beyond quota (output-token ceiling,
        # rate limit, network/timeout, MCP connect, crashes) beside this arm's
        # mean, so the smaller reward-population N is visible. Quota has its own
        # suffix above (the codeprobe-9xrl contract), so only the non-quota
        # remainder is shown here — the three suffixes partition errored_count.
        infra_suffix = ""
        non_quota_infra = s.infra_failure_count - s.quota_error_count
        if non_quota_infra > 0:
            infra_suffix = f" ⚠ {non_quota_infra} infra failure(s)"
        # codeprobe-h3j4: runs excluded from scoring that are NOT infra
        # casualties (a non-executed row the gate does not ask to re-run) —
        # flag the remainder so the headline mean isn't read as the whole story.
        errored_suffix = ""
        other_errored = s.errored_count - s.infra_failure_count
        if s.scored_count > 0 and other_errored > 0:
            errored_suffix = f" ⚠ {other_errored} errored (excluded)"
        # codeprobe-1gg: flag arms where the agent abandoned an enabled tool
        # surface (zero calls on a trial that ran). A nonzero count means
        # this arm's effect is partly "the agent ignored the tooling" — the
        # comparison is INVALID, not a clean null result.
        abandoned_suffix = ""
        if s.abandoned_surface_count > 0:
            abandoned_suffix = (
                f" ⚠ {s.abandoned_surface_count} abandoned-surface trial(s)"
            )
        # codeprobe-f7rl.9: per-arm N (distinct tasks / expected) whenever an
        # expectation exists, plus an explicit PARTIAL flag on incomplete arms
        # so a crashed arm is visible on its own row, not just in the header.
        n_suffix = ""
        partial_suffix = ""
        if s.tasks_expected is not None:
            n_suffix = f" N={s.distinct_task_count}/{s.tasks_expected}"
            if s.is_partial:
                partial_suffix = (
                    f" ⚠ PARTIAL ({s.distinct_task_count}/{s.tasks_expected} tasks)"
                )
        # codeprobe-f7rl.31: surface the stats-layer small-sample warning
        # verbatim so text mode carries the same caution as HTML/CSV. The CIs
        # above stay rendered — small N softens them, it does not erase them.
        small_n_suffix = ""
        if s.sample_size_warning:
            small_n_suffix = (
                f" ⚠ {s.sample_size_warning} — interpret CIs with caution"
            )
        lines.append(
            f"{rc.rank}. {rc.label} — {headline}{dual_suffix}{n_suffix}, "
            f"{cost_str}{quota_suffix}{infra_suffix}{errored_suffix}"
            f"{abandoned_suffix}{partial_suffix}{small_n_suffix} — "
            f"{rc.recommendation}"
        )
    if any(rc.summary.quota_error_count > 0 for rc in report.rankings):
        lines.append("")
        lines.append(
            "> **Quota note:** trials marked with ⚠ hit an OAuth/API "
            "quota limit. They are infrastructure casualties, so they are "
            "EXCLUDED from the mean, pass rate and CIs rather than scored "
            "0.0 (codeprobe-77z). Rerun the affected trials after quota "
            "resets or with API-key billing to restore the full sample."
        )
    if any(rc.summary.abandoned_surface_count > 0 for rc in report.rankings):
        lines.append("")
        lines.append(
            "> **Tool-surface note:** arms marked with an abandoned-surface "
            "warning enabled a tool surface the agent never called on one or "
            "more trials. Any 'tooling effect' for those arms is confounded "
            "by non-use — treat the comparison as INVALID, not a null "
            "result, until the surface is exercised (codeprobe-1gg)."
        )
    # codeprobe-f7rl.35: disclose when any arm's cost coverage is partial or
    # arms differ in dominant provenance — mirrors the quota-note pattern.
    cost_note = _cost_provenance_note(report.summaries)
    if cost_note is not None:
        lines.append("")
        lines.append(f"> {cost_note}")
    lines.append("")

    # codeprobe-77z: infra-failure validity gate. A run holding an unresolved
    # infra casualty is NOT quotable — the verdict and the offending trial ids
    # are surfaced here so the run-closer / writeup step blocks "complete"
    # status until those trials are re-run (or reclassified genuine).
    if report.validity is not None:
        lines.append("### Validity")
        lines.append(report.validity.summary())
        if not report.validity.passed:
            lines.append("")
            lines.append(
                "> **Validity gate FAILED:** the run is NOT quotable. Re-run "
                "the infra-failure trial(s) listed above to 'completed' (or "
                "reclassify them genuine with a reason) before publishing any "
                "mean, ranking, or comparison from this run (codeprobe-77z)."
            )
        lines.append("")

    # Dual Verification Matrix
    if any_dual_tasks and report.config_results:
        all_tasks = [t for cr in report.config_results for t in cr.completed]
        matrix = dual_matrix(all_tasks)
        if matrix is not None:
            lines.append("### Dual Verification Matrix")
            lines.append("|                | Artifact Pass | Artifact Fail |")
            lines.append("|----------------|---------------|---------------|")
            lines.append(
                f"| **Code Pass**  | {matrix.both_pass} ({matrix.both_pass_pct:.1f}%)"
                f"        | {matrix.code_only_pass} ({matrix.code_only_pass_pct:.1f}%)"
                f"        |"
            )
            lines.append(
                f"| **Code Fail**  | {matrix.artifact_only_pass} ({matrix.artifact_only_pass_pct:.1f}%)"
                f"        | {matrix.neither_pass} ({matrix.neither_pass_pct:.1f}%)"
                f"        |"
            )
            lines.append("")

    # Detailed Comparison
    if report.comparisons:
        lines.append("### Detailed Comparison")
        disclosure = _holm_disclosure(report)
        if disclosure is not None:
            lines.append(disclosure)
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
                passed = "Y" if task_passed(task) else "N"
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
    scorable_rankings = [rc for rc in report.rankings if rc.summary.scored_count > 0]
    if scorable_rankings:
        best = scorable_rankings[0]
        lines.append(f"Use {best.label} for best results.")

        cost_efficient = [
            r for r in scorable_rankings if "cost-efficiency" in r.recommendation.lower()
        ]
        if cost_efficient:
            lines.append(f"Consider {cost_efficient[0].label} if cost is a concern.")
    elif report.rankings:
        # codeprobe-h3j4: every config's every run was non-executed. Refuse a
        # "Use X" recommendation — there is no comparison to make — and emit a
        # prescriptive next step instead of a confident pick from vacuous 0.0s.
        total_errored = sum(rc.summary.errored_count for rc in report.rankings)
        n_configs = len(report.rankings)
        lines.append(
            f"No comparison available — all {total_errored} run(s) across "
            f"{n_configs} config(s) errored (the agent never executed; e.g. an "
            "invalid model token or an OAuth/API quota limit). Fix the run "
            "configuration and re-execute before comparing."
        )
    else:
        lines.append("No configurations to recommend.")

    return "\n".join(lines)


def _bucket_tool_call_count(count: int | None) -> str:
    """Bucket an observed tool_call_count into low/medium/high.

    Thresholds are explicit, documented, and deterministic — this is
    structural arithmetic, not semantic judgment, so it is ZFC-compliant
    (see rules/common/patterns.md "deterministic ranking with explicit
    tiebreaker rules" carve-out).

    Buckets:
      count is None or < 0 → ""    (unknown — no data)
      count <= 2          → "low"
      count <= 10         → "medium"
      count > 10          → "high"
    """
    if count is None or count < 0:
        return ""
    if count <= 2:
        return "low"
    if count <= 10:
        return "medium"
    return "high"


_TOOL_BENEFIT_ORDER: dict[str, int] = {"low": 0, "medium": 1, "high": 2}


def _tool_delta_vs_expected(
    expected: str, observed_count: int | None
) -> str:
    """Compute the per-task tool-delta label.

    Returns one of:
      ""       — expected is blank (not assessed) OR observed is unknown
      "match"  — observed bucket == expected
      "under"  — observed bucket strictly below expected
      "over"   — observed bucket strictly above expected
    """
    if expected not in _TOOL_BENEFIT_ORDER:
        return ""
    observed = _bucket_tool_call_count(observed_count)
    if observed == "":
        return ""
    exp_idx = _TOOL_BENEFIT_ORDER[expected]
    obs_idx = _TOOL_BENEFIT_ORDER[observed]
    if obs_idx == exp_idx:
        return "match"
    if obs_idx < exp_idx:
        return "under"
    return "over"


def _extract_expected_tool_benefit(task: CompletedTask) -> str:
    """Read the task's mine-time ``expected_tool_benefit`` if carried.

    Looks in ``task.metadata`` and ``task.scoring_details`` (runners may
    forward the value through either channel). Returns "" when absent so the
    interpret column is blank rather than guessed.
    """
    meta = task.metadata or {}
    if isinstance(meta, dict):
        value = meta.get("expected_tool_benefit", "")
        if isinstance(value, str) and value:
            return value
    details = task.scoring_details or {}
    if isinstance(details, dict):
        value = details.get("expected_tool_benefit", "")
        if isinstance(value, str) and value:
            return value
    return ""


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
            expected_tool_benefit = _extract_expected_tool_benefit(task)
            tool_delta = _tool_delta_vs_expected(
                expected_tool_benefit, task.tool_call_count
            )
            # R17 — per-checkpoint partial-credit map. Emitted by
            # CheckpointScorer and propagated through scoring_details.
            # JSON reports carry the dict directly; CSV writers stringify
            # via json.dumps so it fits in one cell.
            raw_cp = details.get("checkpoint_scores")
            if isinstance(raw_cp, dict) and raw_cp:
                checkpoint_scores: dict[str, float] | None = {
                    str(k): float(v) for k, v in raw_cp.items()
                }
                checkpoint_scores_csv = json.dumps(checkpoint_scores, sort_keys=True)
            else:
                checkpoint_scores = None
                checkpoint_scores_csv = ""
            rows.append(
                {
                    "config": cr.config,
                    "task_id": task.task_id,
                    # 1-based repeat number; repeat_index is 0-based so
                    # single-repeat runs keep emitting repeat=1 unchanged.
                    "repeat": task.repeat_index + 1,
                    "score": task.automated_score,
                    "pass": 1 if task_passed(task) else 0,
                    "duration_sec": task.duration_seconds,
                    "cost_usd": task.cost_usd,
                    "cost_source": task.cost_source,
                    "input_tokens": task.input_tokens,
                    "output_tokens": task.output_tokens,
                    "cache_read_tokens": task.cache_read_tokens,
                    "cache_creation_tokens": task.cache_creation_tokens,
                    "cost_model": task.cost_model,
                    "ci_lower": ci_lower,
                    "ci_upper": ci_upper,
                    "expected_tool_benefit": expected_tool_benefit,
                    "tool_call_count": task.tool_call_count,
                    "tool_delta_vs_expected": tool_delta,
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
                    # R17 per-checkpoint breakdown: dict in memory/JSON, JSON
                    # string in CSV so a single cell captures the full map.
                    "checkpoint_scores": checkpoint_scores,
                    "checkpoint_scores_csv": checkpoint_scores_csv,
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
        # codeprobe-77z: infra-failure validity gate over every trial in the
        # run. ``passed`` is False while any unresolved infra casualty remains;
        # a run-closer consumes this to block "quotable/complete" status. None
        # only for reports built without trial-level data.
        "validity": asdict(report.validity) if report.validity is not None else None,
        # Drop the CSV-helper mirror from the JSON view; the native
        # ``checkpoint_scores`` dict is already present and more useful.
        "tasks": [
            {k: v for k, v in row.items() if k != "checkpoint_scores_csv"}
            for row in _build_task_rows(report)
        ],
    }

    # Add dual matrix when dual tasks are present
    any_dual = any((s.dual_task_count or 0) > 0 for s in report.summaries)
    if any_dual and report.config_results:
        all_tasks = [t for cr in report.config_results for t in cr.completed]
        matrix = dual_matrix(all_tasks)
        if matrix is not None:
            data["dual_matrix"] = {
                "both_pass": {
                    "count": matrix.both_pass,
                    "pct": matrix.both_pass_pct,
                },
                "code_only_pass": {
                    "count": matrix.code_only_pass,
                    "pct": matrix.code_only_pass_pct,
                },
                "artifact_only_pass": {
                    "count": matrix.artifact_only_pass,
                    "pct": matrix.artifact_only_pass_pct,
                },
                "neither_pass": {
                    "count": matrix.neither_pass,
                    "pct": matrix.neither_pass_pct,
                },
                "total": matrix.total,
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
    "cache_creation_tokens",
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
    # Tool-benefit delta — expected level (from metadata.json at mine time)
    # vs the bucketed observed tool_call_count. Empty string when either
    # side is unknown.
    "expected_tool_benefit",
    "tool_call_count",
    "tool_delta_vs_expected",
    # R17 checkpoint scoring — JSON-encoded {step_name: score} dict. Empty
    # for non-checkpoint tasks so the CSV schema stays uniform.
    "checkpoint_scores",
]


def format_html_report(report: Report) -> str:
    """Format report as a self-contained HTML file with inline CSS/JS."""
    parts: list[str] = []

    # codeprobe-h3j4: the "best" config must come from the scorable set —
    # never an all-errored (non-executed) config. None scorable → no pick.
    scorable_rankings = [rc for rc in report.rankings if rc.summary.scored_count > 0]
    best_label = scorable_rankings[0].label if scorable_rankings else "N/A"
    best_rec = scorable_rankings[0].recommendation if scorable_rankings else ""
    small_sample_arms = [s for s in report.summaries if s.sample_size_warning]

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

    def _cost_cell_html(s: ConfigSummary) -> str:
        """Cost cell with coverage/provenance annotation (codeprobe-f7rl.35).

        Full coverage gets a muted '(10/10 trials, api_reported)' note;
        partial coverage gets a warn-badge with the not-comparable flag, the
        same wording as the text ranking line.
        """
        if s.total_cost_usd is None:
            return "—"
        covered = _cost_covered_count(s)
        if s.cost_coverage == 1.0:
            return (
                f"{_fmt_cost(s.total_cost_usd)} "
                f'<span class="ci-metric-label">'
                f"({covered}/{s.scored_count} trials, "
                f"{_esc(_cost_source_breakdown(s))})</span>"
            )
        return (
            f"{_fmt_cost(s.total_cost_usd)} "
            f'<span class="warn-badge">⚠ cost on '
            f"{covered}/{s.scored_count} trials — not comparable</span>"
        )

    def _exclusion_badges_html(s: ConfigSummary) -> str:
        """Render this arm's reward-population exclusions as badges.

        Mirrors ``format_text_report``'s ranking suffixes verbatim so the text,
        JSON and HTML surfaces tell the same story (codeprobe-gu9m). The counts
        are nested supersets — ``errored_count`` ⊇ ``infra_failure_count`` ⊇
        ``quota_error_count`` (see ``ConfigSummary``) — so each badge shows only
        its own remainder and the badges partition the excluded set.
        """
        badges: list[str] = []
        if s.scored_count == 0:
            # codeprobe-h3j4: nothing executed — there is no mean to report.
            badges.append(f"ERRORED ({s.errored_count}) — no runs executed")
        if s.quota_error_count > 0:
            badges.append(f"⚠ {s.quota_error_count} quota error(s)")
        non_quota_infra = s.infra_failure_count - s.quota_error_count
        if non_quota_infra > 0:
            badges.append(f"⚠ {non_quota_infra} infra failure(s)")
        other_errored = s.errored_count - s.infra_failure_count
        if s.scored_count > 0 and other_errored > 0:
            badges.append(f"⚠ {other_errored} errored (excluded)")
        # codeprobe-f7rl.9: flag the incomplete arm in the same eyepath as its
        # mean — worded like the text report's per-arm PARTIAL suffix.
        if s.is_partial and s.tasks_expected is not None:
            badges.append(
                f"⚠ PARTIAL ({s.distinct_task_count}/{s.tasks_expected} tasks)"
            )
        if not badges:
            return "—"
        return " ".join(f'<span class="warn-badge">{_esc(b)}</span>' for b in badges)

    def _ci_bar_html(s: ConfigSummary) -> str:
        """Render the CI bar for a summary, with a small-N badge when needed.

        Per the ConfigSummary contract, ``ci_lower``/``ci_upper`` bound the
        PRIMARY metric: ``mean_score`` for continuous scorers, ``pass_rate``
        for binary ones — so the point marker must read ``score_type`` or it
        renders outside its own interval (codeprobe-f7rl.31). Small samples
        keep their computed CIs; the accurate stats-layer warning is rendered
        alongside the bar, never instead of it.
        """
        lo = s.ci_lower * 100
        hi = s.ci_upper * 100
        if s.score_type == "continuous":
            mid = s.mean_score * 100
            metric = "mean score"
        else:
            mid = s.pass_rate * 100
            metric = "pass rate"
        bar = (
            f'<div class="ci-bar" title="95% CI on {metric}">'
            f'<div class="ci-range" style="left:{lo:.1f}%;width:{hi - lo:.1f}%"></div>'
            f'<div class="ci-point" style="left:{mid:.1f}%"></div>'
            f"</div>"
            f'<span class="ci-metric-label">{metric}</span>'
        )
        if s.sample_size_warning:
            bar += (
                f' <span class="small-sample-badge">'
                f"{_esc(s.sample_size_warning)}</span>"
            )
        return bar

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
.small-sample-banner{background:var(--warning);color:#000;padding:.5rem 1rem;
border-radius:4px;margin-bottom:1rem;font-weight:600}
.small-sample-badge{background:var(--warning);color:#000;padding:2px 8px;
border-radius:4px;font-size:.8rem;font-weight:600}
.ci-metric-label{color:var(--muted);font-size:.75rem}
.warn-badge{display:inline-block;background:var(--warning);color:#000;padding:2px 8px;
border-radius:4px;font-size:.8rem;font-weight:600;margin:1px 0}
.validity-fail{background:#f8d7da;border:1px solid var(--danger);color:#842029;
padding:.5rem 1rem;border-radius:4px;margin-top:.75rem}
table{width:100%;border-collapse:collapse;margin:.5rem 0}
th,td{padding:.5rem .75rem;text-align:left;border-bottom:1px solid var(--border)}
th{background:#e9ecef;font-weight:600;font-size:.85rem;text-transform:uppercase;letter-spacing:.03em}
tr:hover{background:#f1f3f5}
.pass{color:var(--success);font-weight:600}
.fail{color:var(--danger);font-weight:600}
.winner-badge{background:var(--success);color:#fff;padding:2px 8px;border-radius:4px;font-size:.8rem}
.refused-badge{background:var(--danger);color:#fff;padding:2px 8px;border-radius:4px;font-size:.8rem}
.verdict-line{color:var(--muted);font-size:.9rem;margin:.25rem 0 .5rem}
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
        # Same worst-arm wording as the text header (codeprobe-f7rl.9).
        parts.append(
            f'<div class="partial-banner">{_esc(_worst_arm_partial_line(report))}'
            "</div>\n"
        )

    if small_sample_arms:
        # codeprobe-f7rl.31: render the accurate stats-layer warning per arm.
        # The old "Single run — no confidence intervals available" wording was
        # false for 2 <= N < 10: CIs exist and are rendered below.
        per_arm = "; ".join(
            f"{s.label}: {s.sample_size_warning}" for s in small_sample_arms
        )
        parts.append(
            '<div class="small-sample-banner">'
            f"{_esc(per_arm)} — interpret confidence intervals with caution</div>\n"
        )

    # --- Executive Summary ---
    parts.append('<h2 id="executive-summary">Executive Summary</h2>\n')
    parts.append('<div class="card executive">\n')
    if scorable_rankings:
        best_s = scorable_rankings[0].summary
        # codeprobe-f7rl.35: the headline cost carries the same coverage /
        # provenance annotation as the ranking table.
        cost_str = _cost_cell_html(best_s)
        parts.append(
            f"<p><strong>Recommendation:</strong> {_esc(best_label)} — "
            f"{_esc(best_rec)}</p>\n"
        )
        parts.append(
            f"<p>Pass rate: {_fmt_pct(best_s.pass_rate)} | "
            f"Mean score: {_fmt_score(best_s.mean_score)} | "
            f"Cost: {cost_str}</p>\n"
        )
    elif report.rankings:
        # codeprobe-h3j4: every config errored — refuse a recommendation.
        total_errored = sum(rc.summary.errored_count for rc in report.rankings)
        n_configs = len(report.rankings)
        parts.append(
            "<p><strong>No comparison available</strong> — all "
            f"{total_errored} run(s) across {n_configs} config(s) errored "
            "(the agent never executed; e.g. an invalid model token or an "
            "OAuth/API quota limit). Fix the run configuration and re-execute "
            "before comparing.</p>\n"
        )
    else:
        parts.append("<p>No configurations to recommend.</p>\n")
    parts.append("</div>\n")

    # --- Ranking Table ---
    parts.append('<h2 id="ranking-table">Rankings</h2>\n')
    parts.append("<table>\n<thead><tr>")
    parts.append(
        "<th>Rank</th><th>Config</th><th>N</th><th>Pass Rate</th>"
        "<th>Mean Score</th><th>Cost</th><th>Billing</th><th>CI</th>"
        "<th>Exclusions</th>"
    )
    parts.append("</tr></thead>\n<tbody>\n")
    for rc in report.rankings:
        s = rc.summary
        # codeprobe-h3j4 / gu9m: an arm with no scorable run never executed, so
        # it has no mean — show em dashes rather than a vacuous 0.00 / 0%. The
        # Exclusions cell carries the ERRORED count, as the text report does.
        if s.scored_count == 0:
            pass_cell = "—"
            mean_cell = "—"
        else:
            pass_cell = _fmt_pct(s.pass_rate)
            mean_cell = _fmt_score(s.mean_score)
        # codeprobe-f7rl.9: per-arm N — distinct tasks over expected. Em dash
        # when no expectation was supplied (report built without total_tasks).
        if s.tasks_expected is not None:
            n_cell = f"{s.distinct_task_count}/{s.tasks_expected}"
        else:
            n_cell = "—"
        parts.append(
            f"<tr><td>{rc.rank}</td><td>{_esc(rc.label)}</td>"
            f"<td>{n_cell}</td>"
            f"<td>{pass_cell}</td>"
            f"<td>{mean_cell}</td>"
            f"<td>{_cost_cell_html(s)}</td>"
            f"<td>{_esc(s.billing_model)}</td>"
            f"<td>{_ci_bar_html(s)}</td>"
            f"<td>{_exclusion_badges_html(s)}</td></tr>\n"
        )
    parts.append("</tbody>\n</table>\n")

    # --- Validity gate (codeprobe-77z) ---
    # Same content, position and wording as the text report's "### Validity"
    # section: a run holding an unresolved infra casualty is NOT quotable, and
    # the HTML view must say so instead of showing a clean headline mean.
    if report.validity is not None:
        parts.append('<h2 id="validity">Validity</h2>\n')
        parts.append('<div class="card">\n')
        parts.append(f"<p>{_esc(report.validity.summary())}</p>\n")
        if not report.validity.passed:
            parts.append(
                '<div class="validity-fail">'
                "<strong>Validity gate FAILED:</strong> the run is NOT quotable. "
                "Re-run the infra-failure trial(s) listed above to 'completed' "
                "(or reclassify them genuine with a reason) before publishing any "
                "mean, ranking, or comparison from this run (codeprobe-77z)."
                "</div>\n"
            )
        parts.append("</div>\n")

    # --- Dual Verification Matrix ---
    any_dual_tasks_flag = any((s.dual_task_count or 0) > 0 for s in report.summaries)
    if any_dual_tasks_flag and report.config_results:
        all_tasks_html = [t for cr in report.config_results for t in cr.completed]
        matrix_html = dual_matrix(all_tasks_html)
        if matrix_html is not None:
            parts.append(
                '<h2 id="dual-verification-matrix">Dual Verification Matrix</h2>\n'
            )
            parts.append('<div class="card">\n')
            parts.append("<table>\n<thead><tr>")
            parts.append("<th></th><th>Artifact Pass</th><th>Artifact Fail</th>")
            parts.append("</tr></thead>\n<tbody>\n")
            parts.append(
                f"<tr><td><strong>Code Pass</strong></td>"
                f"<td>{matrix_html.both_pass} ({matrix_html.both_pass_pct:.1f}%)</td>"
                f"<td>{matrix_html.code_only_pass} ({matrix_html.code_only_pass_pct:.1f}%)</td></tr>\n"
            )
            parts.append(
                f"<tr><td><strong>Code Fail</strong></td>"
                f"<td>{matrix_html.artifact_only_pass} ({matrix_html.artifact_only_pass_pct:.1f}%)</td>"
                f"<td>{matrix_html.neither_pass} ({matrix_html.neither_pass_pct:.1f}%)</td></tr>\n"
            )
            parts.append("</tbody>\n</table>\n")
            parts.append("</div>\n")

    # --- Per-Task Drill-Down ---
    if report.config_results:
        # Use report-level flag (same as text report) so all configs in a
        # mixed experiment share a uniform column schema.
        any_dual_tasks_html = any(
            (s.dual_task_count or 0) > 0 for s in report.summaries
        )
        parts.append('<h2 id="per-task-drilldown">Per-Task Drill-Down</h2>\n')
        for cr in report.config_results:
            parts.append(f"<details>\n<summary>{_esc(cr.config)}</summary>\n")
            parts.append("<table>\n<thead><tr>")
            if any_dual_tasks_html:
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
                passed = task_passed(task)
                cls = "pass" if passed else "fail"
                if any_dual_tasks_html:
                    details = task.scoring_details or {}
                    if "score_artifact" in details:
                        artifact_cell = _fmt_score(float(details["score_artifact"]))
                    else:
                        artifact_cell = "\u2014"
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
        disclosure = _holm_disclosure(report)
        if disclosure is not None:
            parts.append(f"<p>{_esc(disclosure)}</p>\n")
        parts.append('<div class="pairwise-grid">\n')
        for c in report.comparisons:
            parts.append('<div class="pairwise-card">\n')
            # REFUSED pairs (locked decision 6, epic codeprobe-f7rl) get a
            # danger badge instead of a winner. Comparable pairs get the green
            # Winner badge ONLY on a clean "X wins" verdict — a softened
            # verdict ("effectively tied", "nominally ahead (…)") renders as a
            # warning badge instead, so noise is never upgraded to a badged
            # winner in the forwarded artifact (codeprobe-f7rl.31).
            if not c.comparable:
                badge = '<span class="refused-badge">NOT COMPARABLE</span>'
            elif c.verdict == f"{c.winner} wins":
                badge = f'<span class="winner-badge">Winner: {_esc(c.winner)}</span>'
            else:
                badge = f'<span class="warn-badge">{_esc(c.verdict)}</span>'
            parts.append(
                f"<h4>{_esc(c.config_a)} vs {_esc(c.config_b)} {badge}</h4>\n"
            )
            parts.append(f'<p class="verdict-line">{_esc(c.summary)}</p>\n')
            parts.append(
                f'<div class="stat-row"><span class="stat-label">Score diff</span>'
                f'<span class="stat-value">{c.score_diff:+.3f}</span></div>\n'
            )
            if not c.comparable:
                # Reference-only card: the refusal already carries the reason;
                # effect/p/CI are suppressed (never computed for refused pairs).
                parts.append("</div>\n")
                continue
            if c.effect_size is not None:
                parts.append(
                    f'<div class="stat-row"><span class="stat-label">'
                    f"Effect size ({_esc(c.effect_size_method)})</span>"
                    f'<span class="stat-value">{c.effect_size:.3f}</span></div>\n'
                )
            if c.p_value is not None:
                if c.correction == "holm" and c.p_value_adjusted is not None:
                    # Holm-corrected family: the adjusted value is the one
                    # verdicts are gated on; the raw value stays visible
                    # (codeprobe-f7rl.10).
                    parts.append(
                        f'<div class="stat-row"><span class="stat-label">'
                        f"p-value (Holm-adj.)</span>"
                        f'<span class="stat-value">{c.p_value_adjusted:.4f} '
                        f"(raw {c.p_value:.4f})</span></div>\n"
                    )
                else:
                    parts.append(
                        f'<div class="stat-row"><span class="stat-label">p-value</span>'
                        f'<span class="stat-value">{c.p_value:.4f}</span></div>\n'
                    )
            # Always rendered — small samples soften CIs, they don't erase
            # them (codeprobe-f7rl.31). The interval bounds the paired score
            # difference computed in compare_configs.
            parts.append(
                f'<div class="stat-row"><span class="stat-label">'
                f"CI (score diff)</span>"
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
            # codeprobe-h3j4 / codeprobe-f7rl.35: an arm with no scorable run
            # has no pass rate — em dash, matching the ranking table, instead
            # of a vacuous 0%.
            pass_cell = "—" if s.scored_count == 0 else _fmt_pct(s.pass_rate)
            rows.append(
                f"<tr><td>{_esc(s.label)}</td>"
                f"<td>{_cost_cell_html(s)}</td>"
                f"<td>{cost_per_task}</td>"
                f"<td>{pass_cell}</td></tr>\n"
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

    # codeprobe-f7rl.31: accurate small-N wording — CIs are computed and
    # present for 2 <= N < 10, they just deserve caution, so the old
    # "SINGLE RUN — no statistical confidence" comment was false.
    has_warning = any(s.sample_size_warning for s in report.summaries)
    if has_warning:
        buf.write("# SMALL SAMPLE (N<10) — interpret confidence intervals with caution\n")

    writer = csv.DictWriter(buf, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in _build_task_rows(report):
        # CSV uses the stringified ``checkpoint_scores_csv`` for the
        # ``checkpoint_scores`` column so a nested dict fits a single cell
        # without the JSON-dump surprises of Python's default repr().
        csv_source = dict(row)
        csv_source["checkpoint_scores"] = row.get("checkpoint_scores_csv", "")
        # Replace None with empty string for optional dual columns so the CSV
        # shows a blank cell rather than the string "None".
        csv_row = {
            k: ("" if csv_source.get(k) is None else csv_source.get(k))
            for k in _CSV_COLUMNS
        }
        writer.writerow(csv_row)

    return buf.getvalue()
