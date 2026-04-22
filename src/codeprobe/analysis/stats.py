"""Statistical analysis for experiment configurations."""

from __future__ import annotations

import logging
import math
import statistics
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from codeprobe.models.experiment import CompletedTask, ConfigResults

logger = logging.getLogger(__name__)

# A task is considered "passed" when its automated_score meets or exceeds
# this threshold. Scores are typically 0.0 (fail) or 1.0 (pass), but
# partial scores are supported — anything below this is treated as a fail.
PASS_THRESHOLD = 0.5

_SMALL_SAMPLE_THRESHOLD = 10

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


@dataclass(frozen=True)
class PairwiseComparison:
    """Statistical comparison between two configurations."""

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


def summarize_config(
    results: ConfigResults, *, total_tasks: int | None = None
) -> ConfigSummary:
    """Compute summary statistics for a single config's results.

    When *total_tasks* is provided and exceeds the number of completed tasks,
    the summary is flagged as partial.
    """
    tasks = results.completed
    total = len(tasks)

    is_partial = total_tasks is not None and total < total_tasks

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
        )

    completed_tasks = [t for t in tasks if t.status == "completed"]
    errored_tasks = [t for t in tasks if t.status != "completed"]

    scores = [t.automated_score for t in tasks]
    passed = sum(1 for t in tasks if task_passed(t))
    pass_rate = passed / total

    mean_score = statistics.mean(scores)
    median_score = statistics.median(scores)

    durations = [t.duration_seconds for t in tasks]
    total_duration = sum(durations)
    mean_duration = statistics.mean(durations)

    costs = [t.cost_usd for t in tasks if t.cost_usd is not None]
    total_cost: float | None = sum(costs) if costs else None

    tokens = [
        (t.input_tokens or 0) + (t.output_tokens or 0)
        for t in tasks
        if t.input_tokens is not None or t.output_tokens is not None
    ]
    total_tokens: int | None = sum(tokens) if tokens else None

    ci_lo, ci_hi, score_type = _choose_summary_ci(scores, passed, total)
    warning = (
        f"Small sample size (N={total})" if total < _SMALL_SAMPLE_THRESHOLD else None
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
        ci_lower=ci_lo,
        ci_upper=ci_hi,
        score_type=score_type,
        billing_model=billing,
        sample_size_warning=warning,
        dual_task_count=dual_count,
        direct_pass_rate=direct_rate,
        artifact_pass_rate=artifact_rate,
    )


def summarize_completed_tasks(
    label: str,
    tasks: Iterator[CompletedTask],
    *,
    total_tasks: int | None = None,
) -> ConfigSummary:
    """Compute summary statistics from an iterator of tasks (single-pass).

    Unlike summarize_config() which requires a ConfigResults with a list,
    this accepts an arbitrary iterator and accumulates in one pass without
    buffering all tasks in memory. Produces identical output to
    summarize_config() for the same data.

    When *total_tasks* is provided and exceeds the number of consumed tasks,
    the summary is flagged as partial.
    """
    total = 0
    completed_count = 0
    errored_count = 0
    passed = 0
    token_sum = 0
    has_tokens = False

    scores: list[float] = []
    durations: list[float] = []
    costs: list[float] = []
    billing_models: list[str] = []

    dual_count = 0
    direct_passes = 0
    artifact_passes = 0

    for task in tasks:
        total += 1
        if task.status == "completed":
            completed_count += 1
        else:
            errored_count += 1

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

    is_partial = total_tasks is not None and total < total_tasks

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
        )

    total_duration = sum(durations)
    total_cost: float | None = sum(costs) if costs else None

    ci_lo, ci_hi, score_type = _choose_summary_ci(scores, passed, total)
    warning = (
        f"Small sample size (N={total})" if total < _SMALL_SAMPLE_THRESHOLD else None
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
        errored=errored_count,
        pass_rate=passed / total,
        mean_score=statistics.mean(scores),
        median_score=statistics.median(scores),
        total_duration_sec=total_duration,
        mean_duration_sec=statistics.mean(durations),
        total_cost_usd=total_cost,
        total_tokens=token_sum if has_tokens else None,
        is_partial=is_partial,
        tasks_expected=total_tasks,
        ci_lower=ci_lo,
        ci_upper=ci_hi,
        score_type=score_type,
        billing_model=billing,
        sample_size_warning=warning,
        dual_task_count=dual_count,
        direct_pass_rate=direct_rate,
        artifact_pass_rate=artifact_rate,
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


def compare_configs(
    a: ConfigSummary,
    b: ConfigSummary,
    *,
    a_scores: Sequence[float] | None = None,
    b_scores: Sequence[float] | None = None,
) -> PairwiseComparison:
    """Compare two configurations and determine which is better.

    When *a_scores* and *b_scores* are provided (paired per-task scores),
    statistical hypothesis tests and effect sizes are computed.
    """
    score_diff = a.mean_score - b.mean_score

    cost_diff: float | None = None
    if a.total_cost_usd is not None and b.total_cost_usd is not None:
        cost_diff = a.total_cost_usd - b.total_cost_usd

    speed_diff = a.mean_duration_sec - b.mean_duration_sec
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

    # Build human-readable summary
    parts: list[str] = []
    parts.append(f"{score_diff:+.0%} score")
    if cost_diff is not None:
        parts.append(f"{cost_diff:+.2f} cost")
    if speed_diff < 0:
        parts.append(f"{abs(speed_diff):.1f}s faster")
    elif speed_diff > 0:
        parts.append(f"{speed_diff:.1f}s slower")

    summary = f"{a.label} vs {b.label}: {', '.join(parts)} " f"\u2192 {winner} wins"

    return PairwiseComparison(
        config_a=a.label,
        config_b=b.label,
        score_diff=score_diff,
        cost_diff=cost_diff,
        speed_diff=speed_diff,
        winner=winner,
        summary=summary,
        p_value=p_val,
        effect_size=eff_size,
        effect_size_method=eff_method,
        ci_lower=ci_lo,
        ci_upper=ci_hi,
    )
