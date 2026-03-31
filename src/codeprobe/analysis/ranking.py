"""Ranking and recommendations for experiment configurations."""

from __future__ import annotations

from dataclasses import dataclass

from codeprobe.analysis.stats import ConfigSummary


@dataclass(frozen=True)
class RankedConfig:
    """A config with its rank and recommendation."""

    rank: int
    label: str
    summary: ConfigSummary
    recommendation: str


def _ordinal(n: int) -> str:
    """Return ordinal string for an integer (1st, 2nd, 3rd, etc.)."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def rank_configs(summaries: list[ConfigSummary]) -> list[RankedConfig]:
    """Rank configs by score (primary), cost-efficiency (secondary), speed (tertiary).

    Generate a recommendation string for each:
    - Rank 1 with score > 0.7: "Best overall — high pass rate"
    - Rank 1 with score <= 0.7: "Best available — consider more tasks"
    - Has lowest cost and score within 10% of best: "Best cost-efficiency"
    - Score == 0: "Not recommended — no tasks passed"
    - Otherwise: ordinal position summary
    """
    if not summaries:
        return []

    # Sort: higher score first, then lower cost, then lower duration
    sorted_summaries = sorted(
        summaries,
        key=lambda s: (
            -s.mean_score,
            s.total_cost_usd if s.total_cost_usd is not None else float("inf"),
            s.mean_duration_sec,
        ),
    )

    best_score = sorted_summaries[0].mean_score

    # Find lowest-cost config
    configs_with_cost = [s for s in sorted_summaries if s.total_cost_usd is not None]
    lowest_cost_label: str | None = None
    if configs_with_cost:
        lowest_cost = min(configs_with_cost, key=lambda s: s.total_cost_usd)  # type: ignore[arg-type]
        lowest_cost_label = lowest_cost.label

    ranked: list[RankedConfig] = []
    for i, summary in enumerate(sorted_summaries, start=1):
        recommendation = _build_recommendation(
            rank=i,
            summary=summary,
            best_score=best_score,
            lowest_cost_label=lowest_cost_label,
        )
        ranked.append(
            RankedConfig(
                rank=i,
                label=summary.label,
                summary=summary,
                recommendation=recommendation,
            )
        )

    return ranked


def _build_recommendation(
    *,
    rank: int,
    summary: ConfigSummary,
    best_score: float,
    lowest_cost_label: str | None,
) -> str:
    """Build recommendation string for a ranked config."""
    if summary.mean_score == 0:
        return "Not recommended — no tasks passed"

    if rank == 1:
        if summary.mean_score > 0.7:
            return "Best overall — high pass rate"
        return "Best available — consider more tasks"

    # Check cost-efficiency: lowest cost and score within 10% of best
    if (
        lowest_cost_label is not None
        and summary.label == lowest_cost_label
        and best_score > 0
        and summary.mean_score >= best_score * 0.9
    ):
        return "Best cost-efficiency"

    return f"Ranked {_ordinal(rank)} overall"
