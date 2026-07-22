"""Statistical analysis for experiment configurations."""

from __future__ import annotations

import logging
import math
import statistics
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from codeprobe.analysis.validity import is_infra_failure
from codeprobe.models.experiment import (
    CompletedTask,
    ConfigResults,
    ExperimentConfig,
)

logger = logging.getLogger(__name__)

# A task is considered "passed" when its automated_score meets or exceeds
# this threshold. Scores are typically 0.0 (fail) or 1.0 (pass), but
# partial scores are supported — anything below this is treated as a fail.
PASS_THRESHOLD = 0.5

_SMALL_SAMPLE_THRESHOLD = 10

# Minimum shared (paired) tasks for a pairwise verdict. Below 3 shared tasks
# no paired test in this module has meaningful power (wilcoxon_test already
# returns None for n < 2), so compare_configs REFUSES the verdict instead of
# caveating it (locked decision 6, epic codeprobe-f7rl).
_MIN_PAIRED_TASKS = 3

# Import AFTER PASS_THRESHOLD is defined: dual.py defers its own stats
# import to function bodies, so this direction is the only safe one.
from codeprobe.analysis.dual import (  # noqa: E402
    _strict_bool,
    has_dual_scoring,
    resolve_leg_pass,
)

# ---------------------------------------------------------------------------
# Pass/fail predicate — single source of truth
# ---------------------------------------------------------------------------


def score_passed(automated_score: float, scoring_details: dict | None = None) -> bool:
    """Return whether a score represents a pass.

    Prefers the scorer's explicit ``scoring_details['passed']`` flag when
    present (accepting bool or JSON-round-tripped string forms like
    ``"false"``/``"true"`` via :func:`_strict_bool`), else falls back to
    ``automated_score >= PASS_THRESHOLD``.
    """
    details = scoring_details or {}
    explicit = _strict_bool(details.get("passed"))
    if explicit is not None:
        return explicit
    return automated_score >= PASS_THRESHOLD


def task_passed(task: CompletedTask) -> bool:
    """Return whether a completed task passed.

    Thin wrapper around :func:`score_passed` for ``CompletedTask`` objects.
    """
    return score_passed(task.automated_score, task.scoring_details)


def is_quota_casualty(task: CompletedTask) -> bool:
    """Return whether a task is a quota-error infrastructure casualty.

    Quota-errored trials are assigned ``automated_score=0.0`` by the
    executor, but that 0.0 is an unrecoverable infrastructure failure, not
    a task-quality measurement. Such trials are therefore excluded from the
    reward population — ``scores``, ``durations``, and the pass-rate — and
    surfaced separately via ``ConfigSummary.quota_error_count`` instead of
    silently dragging ``mean_score`` toward zero (codeprobe-a8r; the
    ``quota_error_count`` contract is codeprobe-9xrl). Single source of
    truth so both summarizers and the pairwise comparison agree on which
    trials count.
    """
    return task.error_category == "quota"


def is_scorable_run(task: CompletedTask) -> bool:
    """Return whether a run counts toward scoring, ranking, and recommendations.

    A run is *scorable* only when the agent actually executed and produced a
    genuine measurement: ``status == "completed"`` (scored) or
    ``status == "failed"`` (an adapter-declared terminal failure — a real
    0.0-reward measurement, codeprobe-8up). A run is NOT scorable when
    ``status == "error"``: the agent never executed (invalid model token,
    OAuth quota casualty, crash, system fault). The executor stamps those
    non-executed runs with a hard-coded ``automated_score=0.0`` that is an
    infrastructure artifact, not a task-quality measurement, so folding it into
    ``mean_score`` / ``pass_rate`` / rankings manufactures a confident A/B
    comparison out of runs that never ran (codeprobe-h3j4).

    A run is also NOT scorable when it is an infrastructure casualty
    (:func:`codeprobe.analysis.validity.is_infra_failure`) — an output-token
    ceiling overrun, quota/OAuth exhaustion, rate limit, network/timeout, or MCP
    connect failure. That closes the hole the status filter alone leaves open: a
    crash recorded as terminal ``failed`` rather than ``error`` used to keep its
    0.0 in the mean (codeprobe-77z). ``is_infra_failure`` is a strict superset of
    ``is_quota_casualty``, so the earlier quota-exclusion contract
    (codeprobe-9jxx / codeprobe-a8r) is preserved.

    This is the ONE structural predicate (status + fault-signature filter, no
    semantic judgment — ZFC) that the executor-fed summaries, the rankings, the
    pairwise tests, and the CLI terminal summary all route through so they agree
    on which runs count.
    """
    return task.status != "error" and not is_infra_failure(task)


def partition_reward_population(
    tasks: Sequence[CompletedTask],
) -> tuple[list[CompletedTask], int, int]:
    """Split tasks into the scorable reward population and exclusion counts.

    Returns ``(reward_tasks, quota_error_count, errored_count)``:

    * ``reward_tasks`` — runs that count toward scores/durations/pass-rate and
      the published mean (``is_scorable_run``).
    * ``quota_error_count`` — runs lost to an OAuth/API quota limit
      (``is_quota_casualty``); a subset of the excluded set, surfaced
      separately so the quota note stays accurate (codeprobe-9xrl).
    * ``errored_count`` — ALL runs excluded from scoring: non-executed
      (``status == "error"``) plus any infrastructure casualty
      (``is_infra_failure``, codeprobe-77z), i.e.
      ``len(tasks) - len(reward_tasks)`` (codeprobe-h3j4).

    Excluded runs are stamped ``automated_score=0.0`` by the executor, but that
    0.0 is an infrastructure artifact, not a task-quality measurement. Single
    source of truth so every summarizer agrees on which trials count.
    """
    reward_tasks = [t for t in tasks if is_scorable_run(t)]
    quota_error_count = sum(1 for t in tasks if is_quota_casualty(t))
    errored_count = len(tasks) - len(reward_tasks)
    return reward_tasks, quota_error_count, errored_count


# ---------------------------------------------------------------------------
# Statistical helper functions
# ---------------------------------------------------------------------------


def wilson_ci(passed: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion."""
    if total == 0:
        return (0.0, 0.0)
    p = passed / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
    return ((centre - spread) / denom, (centre + spread) / denom)


def mcnemars_exact_test(
    a_scores: Sequence[float], b_scores: Sequence[float]
) -> float | None:
    """McNemar's exact test for paired binary pass/fail outcomes.

    Returns a two-sided p-value, or None when there are no discordant pairs.
    """
    if len(a_scores) != len(b_scores):
        return None

    # Count discordant pairs
    n01 = 0  # a fail, b pass
    n10 = 0  # a pass, b fail
    for a_s, b_s in zip(a_scores, b_scores):
        a_pass = a_s >= PASS_THRESHOLD
        b_pass = b_s >= PASS_THRESHOLD
        if a_pass and not b_pass:
            n10 += 1
        elif not a_pass and b_pass:
            n01 += 1

    n = n01 + n10
    if n == 0:
        return None

    # Exact binomial test: two-sided p-value under H0: p=0.5
    k = min(n01, n10)
    p_value = 0.0
    for i in range(k + 1):
        p_value += math.comb(n, i) * 0.5**n
    return min(2.0 * p_value, 1.0)


def wilcoxon_test(a_scores: Sequence[float], b_scores: Sequence[float]) -> float | None:
    """Wilcoxon signed-rank test for paired continuous scores.

    Returns p-value, or None if scipy is unavailable or all differences are zero.
    """
    if len(a_scores) != len(b_scores) or len(a_scores) < 2:
        return None

    diffs = [a - b for a, b in zip(a_scores, b_scores)]
    if all(d == 0.0 for d in diffs):
        return None

    try:
        from scipy.stats import wilcoxon as _wilcoxon

        result = _wilcoxon(a_scores, b_scores)
        return float(result.pvalue)
    except (ImportError, ValueError):
        return None


def holm_adjusted(p_values: Sequence[float | None]) -> list[float | None]:
    """Holm step-down adjusted p-values for a family of pairwise tests.

    ``None`` entries (untested pairs, e.g. REFUSED comparisons) stay ``None``
    and keep their positions; the family size ``m`` counts only the non-None
    entries. Standard step-down: sort the tested p-values ascending, then
    ``adjusted[i] = max(adjusted so far, (m - rank) * p)`` clamped to 1.0,
    which enforces monotonicity by construction. Deterministic math
    (ZFC-allowed; locked decision 6, epic codeprobe-f7rl).
    """
    tested = [(i, p) for i, p in enumerate(p_values) if p is not None]
    adjusted: list[float | None] = [None] * len(p_values)
    m = len(tested)
    running = 0.0
    for rank, (i, p) in enumerate(sorted(tested, key=lambda ip: ip[1])):
        running = max(running, (m - rank) * p)
        adjusted[i] = min(running, 1.0)
    return adjusted


def cliffs_delta(a: Sequence[float], b: Sequence[float]) -> float:
    """Cliff's delta effect size for ordinal/binary data.

    Returns a value in [-1, 1]. Positive means a > b on average.
    """
    if not a or not b:
        return 0.0
    n = len(a) * len(b)
    more = sum(1 for ai in a for bi in b if ai > bi)
    less = sum(1 for ai in a for bi in b if ai < bi)
    return (more - less) / n


def cohens_d(a: Sequence[float], b: Sequence[float]) -> float:
    """Cohen's d effect size for continuous paired data.

    Uses pooled standard deviation. Returns 0.0 when variance is zero.
    """
    if len(a) < 2 or len(b) < 2:
        return 0.0
    mean_a = statistics.mean(a)
    mean_b = statistics.mean(b)
    var_a = statistics.variance(a)
    var_b = statistics.variance(b)
    pooled_std = math.sqrt((var_a + var_b) / 2)
    if pooled_std == 0.0:
        return 0.0
    return (mean_a - mean_b) / pooled_std


def _is_binary(scores: Sequence[float]) -> bool:
    """Check if scores are binary (only 0.0 and 1.0 values)."""
    return all(s == 0.0 or s == 1.0 for s in scores)


def mean_score_ci(scores: Sequence[float], z: float = 1.96) -> tuple[float, float]:
    """Normal-approximation CI for the sample mean of continuous scores.

    Clamped to [0, 1] because codeprobe scores are bounded. For N < 2 or
    zero-variance samples the interval collapses to (mean, mean).
    """
    n = len(scores)
    if n == 0:
        return (0.0, 0.0)
    mean = statistics.mean(scores)
    if n < 2:
        return (max(0.0, mean), min(1.0, mean))
    try:
        sd = statistics.stdev(scores)
    except statistics.StatisticsError:
        return (max(0.0, mean), min(1.0, mean))
    se = sd / math.sqrt(n)
    lo = max(0.0, mean - z * se)
    hi = min(1.0, mean + z * se)
    return (lo, hi)


def _choose_summary_ci(
    scores: Sequence[float], passed: int, total: int
) -> tuple[float, float, str]:
    """Return (ci_lower, ci_upper, score_type) for a config summary.

    Continuous scorers (any score not in {0.0, 1.0}) get a normal-approx CI
    on the sample mean. Truly binary scorers get the Wilson CI on pass_rate.
    """
    if not scores:
        return 0.0, 0.0, "binary"
    if _is_binary(scores):
        lo, hi = wilson_ci(passed, total)
        return lo, hi, "binary"
    lo, hi = mean_score_ci(scores)
    return lo, hi, "continuous"


def _dominant_billing_model(tasks: Sequence[CompletedTask]) -> str:
    """Return the most common cost_model among tasks, or 'unknown'."""
    models = [t.cost_model for t in tasks if t.cost_model != "unknown"]
    if not models:
        return "unknown"
    counter = Counter(models)
    return counter.most_common(1)[0][0]


def _dual_leg_stats(
    tasks: Sequence[CompletedTask],
) -> tuple[int, float | None, float | None]:
    """Compute ``(dual_task_count, direct_pass_rate, artifact_pass_rate)``.

    Returns ``(0, None, None)`` when no tasks carry dual scoring details.
    Delegates per-task predicates to :mod:`codeprobe.analysis.dual`.
    """
    dual_count = 0
    direct_passes = 0
    artifact_passes = 0
    for task in tasks:
        if not has_dual_scoring(task):
            continue
        dual_count += 1
        direct_pass, artifact_pass = resolve_leg_pass(task)
        if direct_pass:
            direct_passes += 1
        if artifact_pass:
            artifact_passes += 1

    if dual_count == 0:
        return 0, None, None
    return (
        dual_count,
        direct_passes / dual_count,
        artifact_passes / dual_count,
    )


@dataclass(frozen=True)
class ConfigSummary:
    """Aggregated stats for one configuration."""

    label: str
    total_tasks: int
    completed: int
    errored: int
    pass_rate: float
    mean_score: float
    median_score: float
    total_duration_sec: float
    mean_duration_sec: float
    total_cost_usd: float | None
    total_tokens: int | None
    is_partial: bool = False
    tasks_expected: int | None = None
    # Number of unique task_ids observed — the repeat-safe N. With --repeats
    # the trial count (``total_tasks``) exceeds the task count, so partial
    # detection and the rendered "N=x/y" compare THIS against
    # ``tasks_expected``, never the trial count (codeprobe-f7rl.9).
    distinct_task_count: int = 0
    # ``ci_lower`` / ``ci_upper`` bound the *primary metric* for this summary.
    # For binary scorers the primary metric is ``pass_rate`` (Wilson CI);
    # for continuous scorers it's ``mean_score`` (normal-approximation CI
    # on the sample mean). ``score_type`` says which. Renderers should read
    # ``score_type`` to label the interval correctly.
    ci_lower: float = 0.0
    ci_upper: float = 0.0
    score_type: str = "binary"  # "binary" or "continuous"
    billing_model: str = "unknown"
    sample_size_warning: str | None = None
    # Dual scoring leg stats — populated only when tasks carry dual
    # scoring_details. ``dual_task_count`` is the number of dual-scored tasks
    # seen; ``direct_pass_rate`` / ``artifact_pass_rate`` are pass rates
    # computed over that subset. They are ``None`` when no dual data is
    # present so renderers can skip dual-specific columns/rows.
    dual_task_count: int = 0
    direct_pass_rate: float | None = None
    artifact_pass_rate: float | None = None
    # Count of trials whose ``error_category == "quota"`` — broken out
    # separately because quota errors are unrecoverable infrastructure
    # failures, not task-quality failures, and should NOT roll into
    # ``mean_score`` (codeprobe-9xrl). Renderers surface this as a
    # warning so users see how much of the data is contaminated.
    quota_error_count: int = 0
    # Count of trials classified as infrastructure casualties (output-token
    # ceiling, quota/OAuth, rate limit, network/timeout, MCP connect failure,
    # crashes) by ``analysis.validity.is_infra_failure`` — a superset of
    # quota_error_count. These are excluded from the reward population
    # (scores/durations/pass-rate/CIs) so their 0.0 stub never rolls into
    # mean_score, and surfaced here so the exclusion is visible rather than
    # silent (codeprobe-77z; adapter-contract honesty). A nonzero count means
    # the run is NOT quotable until the trials are re-run — see
    # ``codeprobe.analysis.validity.triage_run``.
    infra_failure_count: int = 0
    # Count of runs excluded from scoring: non-executed (status == "error":
    # invalid model token, crash, system fault) plus any infra casualty. These
    # carry a hard-coded automated_score=0.0 that is an infrastructure artifact,
    # not a task-quality measurement, so they are kept out of mean_score /
    # pass_rate / rankings and surfaced as ERRORED (n) instead of as 0.00 failure
    # rows (codeprobe-h3j4). Superset of quota_error_count and of
    # infra_failure_count.
    errored_count: int = 0
    # Count of trials that abandoned at least one ENABLED tool surface —
    # the agent made zero calls into a surface its config declared, on a
    # trial that actually ran (codeprobe-1gg). A nonzero count means this
    # arm's "tooling effect" is partly "the agent ignored the tooling": the
    # comparison is INVALID, not a null result. Zero when no config was
    # supplied to the summarizer (the audit needs the declared surface).
    abandoned_surface_count: int = 0

    @property
    def scored_count(self) -> int:
        """Number of runs that counted toward scoring (executed runs).

        Equals ``total_tasks - errored_count``. Zero means no run produced a
        genuine measurement, so this config cannot be ranked or recommended —
        rankings and the report use this to mark it ERRORED instead of letting
        an all-non-executed config win a comparison on a vacuous 0.0 mean
        (codeprobe-h3j4).
        """
        return self.total_tasks - self.errored_count


@dataclass(frozen=True)
class PairwiseComparison:
    """Statistical comparison between two configurations.

    When ``comparable`` is False the pair was REFUSED (disjoint task sets or
    below the paired-comparison floor): ``winner`` is empty, ``refusal_reason``
    says why, and the diff fields are reference-only data, not a verdict
    (locked decision 6, epic codeprobe-f7rl).
    """

    config_a: str
    config_b: str
    score_diff: float
    cost_diff: float | None
    speed_diff: float
    winner: str
    summary: str
    p_value: float | None = None
    effect_size: float | None = None
    effect_size_method: str = ""
    ci_lower: float = 0.0
    ci_upper: float = 0.0
    comparable: bool = True
    refusal_reason: str = ""
    # Structured verdict phrase from _derive_verdict — the same string that
    # closes ``summary`` ("X wins", "effectively tied", "X nominally ahead
    # (…)"). Renderers gate the Winner badge on this field instead of
    # string-parsing ``summary``; empty for REFUSED pairs (codeprobe-f7rl.31).
    verdict: str = ""
    # Multiple-comparison correction (codeprobe-f7rl.10). For k=2 experiments
    # no correction runs: ``p_value_adjusted`` equals the raw ``p_value`` and
    # ``correction`` stays "none". For k>2 the report layer Holm-corrects the
    # family and re-derives the verdict from the ADJUSTED p; ``n_comparisons``
    # is the family size m (number of tested pairs).
    p_value_adjusted: float | None = None
    correction: str = "none"
    n_comparisons: int = 1


def summarize_config(
    results: ConfigResults,
    *,
    total_tasks: int | None = None,
    config: ExperimentConfig | None = None,
) -> ConfigSummary:
    """Compute summary statistics for a single config's results.

    When *total_tasks* is provided and exceeds the number of DISTINCT task
    ids observed (repeat-safe — trials do not count twice), the summary is
    flagged as partial. When *config* is provided, the
    tool-surface audit (codeprobe-1gg) runs and ``abandoned_surface_count``
    counts trials that ignored an enabled surface.
    """
    tasks = results.completed
    total = len(tasks)

    # Repeat-safe partial detection: compare unique task_ids to the expected
    # task count. Comparing the trial count would break both ways under
    # --repeats — 12 trials on 10 tasks looks complete at 6/10 tasks
    # (codeprobe-f7rl.9).
    distinct = len({t.task_id for t in tasks})
    is_partial = total_tasks is not None and distinct < total_tasks

    if total == 0:
        return ConfigSummary(
            label=results.config,
            total_tasks=0,
            completed=0,
            errored=0,
            pass_rate=0.0,
            mean_score=0.0,
            median_score=0.0,
            total_duration_sec=0.0,
            mean_duration_sec=0.0,
            total_cost_usd=None,
            total_tokens=None,
            is_partial=is_partial,
            tasks_expected=total_tasks,
            distinct_task_count=0,
        )

    completed_tasks = [t for t in tasks if t.status == "completed"]
    errored_tasks = [t for t in tasks if t.status != "completed"]
    # Reward population: executed, non-casualty trials only (non-executed
    # status=="error" runs and infra casualties — quota, token-ceiling
    # overruns, rate limits, network faults, crashes — stay in the structural
    # counts but are excluded from scores/durations/pass-rate, see
    # partition_reward_population). ``errored_count`` is the excluded total;
    # ``infra_count`` is the infra-casualty subset (codeprobe-77z) and
    # ``quota_count`` the quota sub-subset surfaced for the quota note.
    reward_tasks, quota_count, errored_count = partition_reward_population(tasks)
    infra_count = sum(1 for t in tasks if is_infra_failure(t))
    scored_total = len(reward_tasks)
    # Deferred import: tool_surface_audit lives under codeprobe.core, whose
    # package __init__ pulls in the executor → scoring → stats chain. A
    # module-level import here would close that cycle (see the dual import
    # note above), so the audit is imported at call time.
    if config is not None:
        from codeprobe.core.tool_surface_audit import task_abandoned_any_surface

        abandoned_count = sum(
            1 for t in tasks if task_abandoned_any_surface(t, config)
        )
    else:
        abandoned_count = 0

    scores = [t.automated_score for t in reward_tasks]
    passed = sum(1 for t in reward_tasks if task_passed(t))
    pass_rate = passed / scored_total if scored_total else 0.0

    mean_score = statistics.mean(scores) if scores else 0.0
    median_score = statistics.median(scores) if scores else 0.0

    durations = [t.duration_seconds for t in reward_tasks]
    total_duration = sum(durations)
    mean_duration = statistics.mean(durations) if durations else 0.0

    costs = [t.cost_usd for t in tasks if t.cost_usd is not None]
    total_cost: float | None = sum(costs) if costs else None

    tokens = [
        (t.input_tokens or 0) + (t.output_tokens or 0)
        for t in tasks
        if t.input_tokens is not None or t.output_tokens is not None
    ]
    total_tokens: int | None = sum(tokens) if tokens else None

    ci_lo, ci_hi, score_type = _choose_summary_ci(scores, passed, scored_total)
    warning = (
        f"Small sample size (N={scored_total})"
        if scored_total < _SMALL_SAMPLE_THRESHOLD
        else None
    )
    billing = _dominant_billing_model(tasks)
    dual_count, direct_rate, artifact_rate = _dual_leg_stats(tasks)

    return ConfigSummary(
        label=results.config,
        total_tasks=total,
        completed=len(completed_tasks),
        errored=len(errored_tasks),
        pass_rate=pass_rate,
        mean_score=mean_score,
        median_score=median_score,
        total_duration_sec=total_duration,
        mean_duration_sec=mean_duration,
        total_cost_usd=total_cost,
        total_tokens=total_tokens,
        is_partial=is_partial,
        tasks_expected=total_tasks,
        distinct_task_count=distinct,
        ci_lower=ci_lo,
        ci_upper=ci_hi,
        score_type=score_type,
        billing_model=billing,
        sample_size_warning=warning,
        dual_task_count=dual_count,
        direct_pass_rate=direct_rate,
        artifact_pass_rate=artifact_rate,
        quota_error_count=quota_count,
        infra_failure_count=infra_count,
        errored_count=errored_count,
        abandoned_surface_count=abandoned_count,
    )


def summarize_completed_tasks(
    label: str,
    tasks: Iterator[CompletedTask],
    *,
    total_tasks: int | None = None,
    config: ExperimentConfig | None = None,
) -> ConfigSummary:
    """Compute summary statistics from an iterator of tasks (single-pass).

    Unlike summarize_config() which requires a ConfigResults with a list,
    this accepts an arbitrary iterator and accumulates in one pass without
    buffering all tasks in memory. Produces identical output to
    summarize_config() for the same data.

    When *total_tasks* is provided and exceeds the number of DISTINCT task
    ids consumed (repeat-safe — trials do not count twice), the summary is
    flagged as partial.
    """
    total = 0
    completed_count = 0
    non_completed_count = 0
    passed = 0
    token_sum = 0
    has_tokens = False
    # Repeat-safe N — mirrors summarize_config()'s distinct-task counting so
    # the two summarizers stay identical (codeprobe-f7rl.9).
    seen_task_ids: set[str] = set()

    scores: list[float] = []
    durations: list[float] = []
    costs: list[float] = []
    billing_models: list[str] = []

    dual_count = 0
    direct_passes = 0
    artifact_passes = 0
    quota_count = 0
    infra_count = 0
    abandoned_count = 0
    # Deferred import — see summarize_config() for the cycle rationale.
    if config is not None:
        from codeprobe.core.tool_surface_audit import task_abandoned_any_surface

    for task in tasks:
        total += 1
        seen_task_ids.add(task.task_id)
        if task.status == "completed":
            completed_count += 1
        else:
            non_completed_count += 1
        if config is not None and task_abandoned_any_surface(task, config):
            abandoned_count += 1

        # Reward population: executed, non-casualty trials only. Non-executed
        # runs (status=="error") and infra casualties (quota, token-ceiling
        # overruns, rate limits, network faults, crashes) are kept in the
        # cost/token/structural totals but their 0.0 stub is excluded from
        # scores/durations/pass-rate so it never rolls into mean_score
        # (codeprobe-h3j4 + codeprobe-77z). ``infra_count`` is the infra subset
        # and ``quota_count`` the quota sub-subset, surfaced for the notes.
        # Mirrors the exclusion in summarize_config() so the two summarizers
        # stay identical.
        if is_quota_casualty(task):
            quota_count += 1
        if is_infra_failure(task):
            infra_count += 1
        if is_scorable_run(task):
            scores.append(task.automated_score)
            if task_passed(task):
                passed += 1
            durations.append(task.duration_seconds)

        if task.cost_usd is not None:
            costs.append(task.cost_usd)

        if task.input_tokens is not None or task.output_tokens is not None:
            token_sum += (task.input_tokens or 0) + (task.output_tokens or 0)
            has_tokens = True

        if task.cost_model != "unknown":
            billing_models.append(task.cost_model)

        if has_dual_scoring(task):
            dual_count += 1
            direct_pass, artifact_pass = resolve_leg_pass(task)
            if direct_pass:
                direct_passes += 1
            if artifact_pass:
                artifact_passes += 1

    # Repeat-safe partial detection — see summarize_config() (codeprobe-f7rl.9).
    distinct = len(seen_task_ids)
    is_partial = total_tasks is not None and distinct < total_tasks

    if total == 0:
        return ConfigSummary(
            label=label,
            total_tasks=0,
            completed=0,
            errored=0,
            pass_rate=0.0,
            mean_score=0.0,
            median_score=0.0,
            total_duration_sec=0.0,
            mean_duration_sec=0.0,
            total_cost_usd=None,
            total_tokens=None,
            is_partial=is_partial,
            tasks_expected=total_tasks,
            distinct_task_count=0,
        )

    # Number of real (executed, non-casualty) trials — the reward population size.
    scored_total = len(scores)
    total_duration = sum(durations)
    total_cost: float | None = sum(costs) if costs else None

    ci_lo, ci_hi, score_type = _choose_summary_ci(scores, passed, scored_total)
    warning = (
        f"Small sample size (N={scored_total})"
        if scored_total < _SMALL_SAMPLE_THRESHOLD
        else None
    )
    billing = (
        Counter(billing_models).most_common(1)[0][0] if billing_models else "unknown"
    )

    if dual_count > 0:
        direct_rate: float | None = direct_passes / dual_count
        artifact_rate: float | None = artifact_passes / dual_count
    else:
        direct_rate = None
        artifact_rate = None

    return ConfigSummary(
        label=label,
        total_tasks=total,
        completed=completed_count,
        errored=non_completed_count,
        pass_rate=passed / scored_total if scored_total else 0.0,
        mean_score=statistics.mean(scores) if scores else 0.0,
        median_score=statistics.median(scores) if scores else 0.0,
        total_duration_sec=total_duration,
        mean_duration_sec=statistics.mean(durations) if durations else 0.0,
        total_cost_usd=total_cost,
        total_tokens=token_sum if has_tokens else None,
        is_partial=is_partial,
        tasks_expected=total_tasks,
        distinct_task_count=distinct,
        ci_lower=ci_lo,
        ci_upper=ci_hi,
        score_type=score_type,
        billing_model=billing,
        sample_size_warning=warning,
        dual_task_count=dual_count,
        direct_pass_rate=direct_rate,
        artifact_pass_rate=artifact_rate,
        quota_error_count=quota_count,
        infra_failure_count=infra_count,
        errored_count=total - scored_total,
        abandoned_surface_count=abandoned_count,
    )


def _determine_winner(a: ConfigSummary, b: ConfigSummary) -> str:
    """Determine the better config by score, then cost, then speed."""
    if not math.isclose(a.mean_score, b.mean_score, rel_tol=1e-9):
        return a.label if a.mean_score > b.mean_score else b.label

    cost_a = a.total_cost_usd
    cost_b = b.total_cost_usd
    if (
        cost_a is not None
        and cost_b is not None
        and not math.isclose(cost_a, cost_b, rel_tol=1e-9)
    ):
        return a.label if cost_a < cost_b else b.label

    if not math.isclose(a.mean_duration_sec, b.mean_duration_sec, rel_tol=1e-9):
        return a.label if a.mean_duration_sec < b.mean_duration_sec else b.label

    return a.label


def _derive_verdict(
    winner: str,
    score_diff: float,
    effect_size: float | None,
    effect_size_method: str,
    p_value: float | None,
) -> str:
    """Derive the verdict phrase from the comparison statistics.

    Module-level so the report layer can re-run it with a Holm-ADJUSTED
    p-value for k>2 experiments (codeprobe-f7rl.10). Softens the verdict
    when the effect is negligible or the test is underpowered, so we don't
    confidently declare a "winner" on what may be noise. Thresholds:
      Cohen's d: |d| < 0.2 is "negligible" (Cohen 1988).
      Cliff's delta: |delta| < 0.147 is "negligible" (Romano et al. 2006).
      p-value > 0.05: not significant at the conventional threshold.
    """
    scores_tied = abs(score_diff) < 0.01
    negligible_threshold = 0.2 if effect_size_method == "cohens_d" else 0.147
    small_effect = (
        effect_size is not None and abs(effect_size) < negligible_threshold
    )
    not_significant = p_value is not None and p_value > 0.05

    if scores_tied:
        return "effectively tied"
    if small_effect and not_significant:
        return f"{winner} nominally ahead (not significant; small effect)"
    if small_effect:
        return f"{winner} nominally ahead (small effect size)"
    if not_significant:
        return f"{winner} nominally ahead (not significant at p=0.05)"
    return f"{winner} wins"


def _comparison_summary(
    label_a: str,
    label_b: str,
    score_diff: float,
    cost_diff: float | None,
    speed_diff: float,
    verdict: str,
) -> str:
    """Build the one-line human-readable comparison summary."""
    parts = [f"{score_diff:+.0%} score"]
    if cost_diff is not None:
        parts.append(f"{cost_diff:+.2f} cost")
    if speed_diff < 0:
        parts.append(f"{abs(speed_diff):.1f}s faster")
    elif speed_diff > 0:
        parts.append(f"{speed_diff:.1f}s slower")
    return f"{label_a} vs {label_b}: {', '.join(parts)} → {verdict}"


def compare_configs(
    a: ConfigSummary,
    b: ConfigSummary,
    *,
    a_scores: Sequence[float] | None = None,
    b_scores: Sequence[float] | None = None,
) -> PairwiseComparison:
    """Compare two configurations and determine which is better.

    When *a_scores* and *b_scores* are provided (paired per-task scores),
    statistical hypothesis tests and effect sizes are computed. With no
    paired scores (disjoint task sets) or fewer than ``_MIN_PAIRED_TASKS``
    shared tasks the comparison is REFUSED: ``comparable=False``, no winner,
    and the diff fields are reference-only (locked decision 6).
    """
    score_diff = a.mean_score - b.mean_score

    cost_diff: float | None = None
    if a.total_cost_usd is not None and b.total_cost_usd is not None:
        cost_diff = a.total_cost_usd - b.total_cost_usd

    speed_diff = a.mean_duration_sec - b.mean_duration_sec

    # REFUSED verdicts on incomparable arms (locked decision 6, epic
    # codeprobe-f7rl). Without this gate the disjoint case fell through every
    # verdict guard (p_value and effect_size both None) straight to
    # "{winner} wins" — the pair with the LEAST statistical basis made the
    # STRONGEST claim. Refuse before any winner or test is computed; the
    # diffs above stay populated as reference-only data.
    refusal_reason = ""
    if a_scores is None or b_scores is None:
        refusal_reason = "no shared tasks between arms (disjoint task sets)"
    elif len(a_scores) < _MIN_PAIRED_TASKS:
        refusal_reason = (
            f"only {len(a_scores)} shared task(s), below the "
            f"{_MIN_PAIRED_TASKS}-task paired-comparison floor"
        )
    if refusal_reason:
        return PairwiseComparison(
            config_a=a.label,
            config_b=b.label,
            score_diff=score_diff,
            cost_diff=cost_diff,
            speed_diff=speed_diff,
            winner="",
            summary=(
                f"{a.label} vs {b.label}: NOT COMPARABLE — {refusal_reason}; "
                "no verdict (per-arm means are reference only)"
            ),
            comparable=False,
            refusal_reason=refusal_reason,
        )

    winner = _determine_winner(a, b)

    # Statistical tests when raw scores are available
    p_val: float | None = None
    eff_size: float | None = None
    eff_method = ""
    ci_lo = 0.0
    ci_hi = 0.0

    if a_scores is not None and b_scores is not None and len(a_scores) == len(b_scores):
        binary = _is_binary(a_scores) and _is_binary(b_scores)
        if binary:
            p_val = mcnemars_exact_test(a_scores, b_scores)
            eff_size = cliffs_delta(list(a_scores), list(b_scores))
            eff_method = "cliffs_delta"
        else:
            p_val = wilcoxon_test(a_scores, b_scores)
            eff_size = cohens_d(list(a_scores), list(b_scores))
            eff_method = "cohens_d"

        # CI for score difference (normal approximation)
        diffs = [ai - bi for ai, bi in zip(a_scores, b_scores)]
        n = len(diffs)
        if n >= 2:
            mean_diff = statistics.mean(diffs)
            se = statistics.stdev(diffs) / math.sqrt(n)
            ci_lo = mean_diff - 1.96 * se
            ci_hi = mean_diff + 1.96 * se

    verdict = _derive_verdict(winner, score_diff, eff_size, eff_method, p_val)
    summary = _comparison_summary(
        a.label, b.label, score_diff, cost_diff, speed_diff, verdict
    )

    return PairwiseComparison(
        config_a=a.label,
        config_b=b.label,
        score_diff=score_diff,
        cost_diff=cost_diff,
        speed_diff=speed_diff,
        winner=winner,
        summary=summary,
        verdict=verdict,
        p_value=p_val,
        effect_size=eff_size,
        effect_size_method=eff_method,
        ci_lower=ci_lo,
        ci_upper=ci_hi,
        # Uncorrected single-pair default: adjusted == raw. The report layer
        # overwrites this for k>2 families (codeprobe-f7rl.10).
        p_value_adjusted=p_val,
    )
