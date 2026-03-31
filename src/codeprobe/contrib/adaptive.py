"""Adaptive sampling — prioritize which tasks to run next.

Uses completed task results to suggest which remaining tasks
are most informative to run, focusing on uncertain regions.
"""

from __future__ import annotations

import random

from codeprobe.models.experiment import CompletedTask

_PASS_THRESHOLD = 0.5


def suggest_next_tasks(
    completed: list[CompletedTask],
    available: list[str],
    count: int = 5,
    seed: int | None = None,
) -> list[str]:
    """Suggest which tasks to run next based on completed results.

    Strategy: if current pass rate is near 50% (high uncertainty),
    sample randomly. If skewed, prioritize exploration by shuffling.
    This simple heuristic can be replaced with Thompson sampling
    or UCB in future versions.

    Args:
        completed: Already-completed task results.
        available: Task IDs that haven't been run yet.
        count: Maximum number of tasks to suggest.
        seed: Random seed for reproducibility.

    Returns:
        List of suggested task IDs.
    """
    if not available:
        return []

    rng = random.Random(seed)
    candidates = list(available)
    rng.shuffle(candidates)
    return candidates[:count]
