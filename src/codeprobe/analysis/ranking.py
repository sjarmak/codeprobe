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

    Only configs with at least one *scorable* run (``scored_count > 0``) are
    ranked by score; configs whose every run was non-executed (``status ==
    "error"`` — invalid model token, quota, crash) are marked ERRORED and
    appended after the ranked ones, excluded from ``best_score`` and from any
    "best" recommendation so a config that never ran cannot win a comparison on
    a vacuous 0.0 mean (codeprobe-h3j4). When NO config is scorable the result
    is all-ERRORED rows and the report refuses a recommendation.

    Recommendation string for each ranked (scorable) config:
    - Rank 1 with score > 0.7: "Best overall — high pass rate"
    - Rank 1 with score <= 0.7: "Best available — consider more tasks"
    - Has lowest cost and score within 10% of best: "Best cost-efficiency"
    - Score == 0: "Not recommended — no tasks passed"
    - Otherwise: ordinal position summary
    """
    if not summaries:
        return []

    scorable = [s for s in summaries if s.scored_count > 0]
    errored = [s for s in summaries if s.scored_count == 0]

    # Sort scorable: higher score first, then lower cost, then lower duration
    sorted_scorable = sorted(
        scorable,
        key=lambda s: (
            -s.mean_score,
            s.total_cost_usd if s.total_cost_usd is not None else float("inf"),
            s.mean_duration_sec,
        ),
    )

    best_score = sorted_scorable[0].mean_score if sorted_scorable else 0.0

    # Find lowest-cost config among the scorable ones
    configs_with_cost = [s for s in sorted_scorable if s.total_cost_usd is not None]
    lowest_cost_label: str | None = None
    if configs_with_cost:
        lowest_cost = min(configs_with_cost, key=lambda s: s.total_cost_usd or 0.0)
        lowest_cost_label = lowest_cost.label

    ranked: list[RankedConfig] = []
    rank = 1
    for summary in sorted_scorable:
        recommendation = _build_recommendation(
            rank=rank,
            summary=summary,
            best_score=best_score,
            lowest_cost_label=lowest_cost_label,
        )
        ranked.append(
            RankedConfig(
                rank=rank,
                label=summary.label,
                summary=summary,
                recommendation=recommendation,
            )
        )
        rank += 1

    # ERRORED configs trail the ranking with a prescriptive, non-success
    # recommendation — never a "best" claim.
    for summary in errored:
        ranked.append(
            RankedConfig(
                rank=rank,
                label=summary.label,
                summary=summary,
                recommendation=_build_errored_recommendation(summary),
            )
        )
        rank += 1

    return ranked


def _build_errored_recommendation(summary: ConfigSummary) -> str:
    """Recommendation for a config with no scorable run (all non-executed)."""
    return (
        f"ERRORED — {summary.errored_count} run(s) did not execute; "
        "excluded from scoring"
    )


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
