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


def _rankable_cost(summary: ConfigSummary) -> float | None:
    """Return a cost usable for ranking decisions, or None.

    A total is only decision-grade when every scorable trial captured cost
    (``cost_coverage == 1.0``). Partial coverage means the total is an
    undercount of unknown size, so it must not order the ranking or earn
    "Best cost-efficiency" (codeprobe-f7rl.35, locked decision 6).
    """
    if summary.total_cost_usd is None or summary.cost_coverage < 1.0:
        return None
    return summary.total_cost_usd


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
    - Has lowest FULLY-covered cost (``cost_coverage == 1.0``) and score
      within 10% of best: "Best cost-efficiency" — partial cost coverage can
      never earn this tag (codeprobe-f7rl.35)
    - Score == 0: "Not recommended — no tasks passed"
    - Otherwise: ordinal position summary
    """
    if not summaries:
        return []

    scorable = [s for s in summaries if s.scored_count > 0]
    errored = [s for s in summaries if s.scored_count == 0]

    # Sort scorable: higher score first, then lower cost, then lower duration.
    # Only fully cost-covered totals participate in the cost tiebreak — a
    # partially-covered arm sorts as if it had no cost data at all
    # (codeprobe-f7rl.35).
    def _sort_key(s: ConfigSummary) -> tuple[float, float, float]:
        cost = _rankable_cost(s)
        return (
            -s.mean_score,
            cost if cost is not None else float("inf"),
            s.mean_duration_sec,
        )

    sorted_scorable = sorted(scorable, key=_sort_key)

    best_score = sorted_scorable[0].mean_score if sorted_scorable else 0.0

    # Find the lowest-cost config among the scorable ones with decision-grade
    # (fully covered) cost, so "Best cost-efficiency" can never be earned on a
    # partial total (codeprobe-f7rl.35).
    with_rankable_cost = [
        (s, cost)
        for s in sorted_scorable
        if (cost := _rankable_cost(s)) is not None
    ]
    lowest_cost_label: str | None = None
    if with_rankable_cost:
        lowest_cost_label = min(with_rankable_cost, key=lambda sc: sc[1])[0].label

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
