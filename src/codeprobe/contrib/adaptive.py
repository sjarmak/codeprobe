"""Adaptive sampling — prioritize which tasks to run next.

Suggests which remaining tasks are most informative to run,
focusing on uncertain regions.
"""

from __future__ import annotations

import random


def suggest_next_tasks(
    available: list[str],
    count: int = 5,
    seed: int | None = None,
) -> list[str]:
    """Suggest which tasks to run next.

    Strategy: shuffle and pick up to *count* tasks. This simple
    heuristic can be replaced with Thompson sampling or UCB in
    future versions.

    Args:
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
