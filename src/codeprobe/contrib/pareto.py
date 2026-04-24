"""Pareto front analysis — find configs that aren't dominated.

A config is on the Pareto front if no other config is both
cheaper AND higher-scoring. Useful for cost-quality trade-off analysis.
"""

from __future__ import annotations

from dataclasses import dataclass

from codeprobe.contrib._shared import PASS_THRESHOLD as _PASS_THRESHOLD
from codeprobe.models.experiment import ConfigResults


@dataclass(frozen=True)
class ParetoPoint:
    """A point on the score-cost plane."""

    label: str
    score: float
    cost: float


def pareto_front(configs: list[ConfigResults]) -> list[ParetoPoint]:
    """Compute the Pareto front over (score, -cost).

    A config is Pareto-optimal if no other config has both
    higher score and lower cost.

    Args:
        configs: Config results with cost information in completed tasks.

    Returns:
        List of Pareto-optimal points, sorted by score descending.
    """
    points: list[ParetoPoint] = []
    for cr in configs:
        n = len(cr.completed)
        if n == 0:
            continue
        # Skip configs where any task has unknown cost (None).
        # None means cost is unavailable (e.g. subscription model),
        # distinct from 0.0 which means legitimately free.
        if any(t.cost_usd is None for t in cr.completed):
            continue
        score = (
            sum(1.0 for t in cr.completed if t.automated_score >= _PASS_THRESHOLD) / n
        )
        cost = sum((t.cost_usd or 0.0) for t in cr.completed)
        points.append(ParetoPoint(label=cr.config, score=score, cost=cost))

    front: list[ParetoPoint] = []
    for p in points:
        dominated = any(
            other.score >= p.score
            and other.cost <= p.cost
            and (other.score > p.score or other.cost < p.cost)
            for other in points
            if other.label != p.label
        )
        if not dominated:
            front.append(p)

    return sorted(front, key=lambda p: -p.score)
