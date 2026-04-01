"""Structured debate — build arguments for/against each config.

Generates a verdict with supporting evidence by comparing
pass rates, costs, and task-level differences.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codeprobe.contrib._shared import PASS_THRESHOLD as _PASS_THRESHOLD
from codeprobe.models.experiment import ConfigResults


@dataclass(frozen=True)
class Verdict:
    """Outcome of a structured config comparison."""

    winner: str  # config label or "tie"
    arguments: list[str] = field(default_factory=list)


def compare_configs(a: ConfigResults, b: ConfigResults) -> Verdict:
    """Build a structured comparison between two configurations.

    Produces arguments based on pass rate, shared task wins,
    and cost efficiency.
    """
    scores_a = {t.task_id: t for t in a.completed}
    scores_b = {t.task_id: t for t in b.completed}

    shared = set(scores_a.keys()) & set(scores_b.keys())
    arguments: list[str] = []

    if not shared:
        return Verdict(winner="tie", arguments=["No shared tasks to compare."])

    pass_a = sum(
        1 for tid in shared if scores_a[tid].automated_score >= _PASS_THRESHOLD
    )
    pass_b = sum(
        1 for tid in shared if scores_b[tid].automated_score >= _PASS_THRESHOLD
    )
    n = len(shared)

    rate_a = pass_a / n
    rate_b = pass_b / n

    arguments.append(f"{a.config} pass rate: {rate_a:.0%} ({pass_a}/{n})")
    arguments.append(f"{b.config} pass rate: {rate_b:.0%} ({pass_b}/{n})")

    wins_a = sum(
        1
        for tid in shared
        if scores_a[tid].automated_score > scores_b[tid].automated_score
    )
    wins_b = sum(
        1
        for tid in shared
        if scores_b[tid].automated_score > scores_a[tid].automated_score
    )
    arguments.append(
        f"Head-to-head: {a.config} wins {wins_a}, {b.config} wins {wins_b}"
    )

    if rate_a > rate_b:
        winner = a.config
    elif rate_b > rate_a:
        winner = b.config
    else:
        winner = "tie"

    return Verdict(winner=winner, arguments=arguments)
