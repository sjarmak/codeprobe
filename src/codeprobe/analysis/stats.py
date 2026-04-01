"""Statistical analysis for experiment configurations."""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterator
from dataclasses import dataclass

from codeprobe.models.experiment import CompletedTask, ConfigResults

# A task is considered "passed" when its automated_score meets or exceeds
# this threshold. Scores are typically 0.0 (fail) or 1.0 (pass), but
# partial scores are supported — anything below this is treated as a fail.
PASS_THRESHOLD = 0.5


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
    passed = sum(1 for s in scores if s >= PASS_THRESHOLD)
    pass_rate = passed / total

    mean_score = statistics.mean(scores)
    median_score = statistics.median(scores)

    durations = [t.duration_seconds for t in tasks]
    total_duration = sum(durations)
    mean_duration = statistics.mean(durations)

    costs = [t.cost_usd for t in tasks if t.cost_usd is not None]
    total_cost: float | None = sum(costs) if costs else None

    tokens = [t.token_count for t in tasks if t.token_count is not None]
    total_tokens: int | None = sum(tokens) if tokens else None

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

    for task in tasks:
        total += 1
        if task.status == "completed":
            completed_count += 1
        else:
            errored_count += 1

        scores.append(task.automated_score)
        if task.automated_score >= PASS_THRESHOLD:
            passed += 1

        durations.append(task.duration_seconds)

        if task.cost_usd is not None:
            costs.append(task.cost_usd)

        if task.token_count is not None:
            token_sum += task.token_count
            has_tokens = True

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


def compare_configs(a: ConfigSummary, b: ConfigSummary) -> PairwiseComparison:
    """Compare two configurations and determine which is better."""
    score_diff = a.mean_score - b.mean_score

    cost_diff: float | None = None
    if a.total_cost_usd is not None and b.total_cost_usd is not None:
        cost_diff = a.total_cost_usd - b.total_cost_usd

    speed_diff = a.mean_duration_sec - b.mean_duration_sec
    winner = _determine_winner(a, b)

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
    )
