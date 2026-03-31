"""Agent fingerprinting — characterize an agent's pass/fail signature.

Creates a binary vector from task results, enabling quick similarity
comparisons between agent configurations.
"""

from __future__ import annotations

from codeprobe.models.experiment import CompletedTask


def fingerprint(tasks: list[CompletedTask]) -> tuple[float, ...]:
    """Create a fingerprint vector from task scores.

    Returns a tuple of scores in task_id order, suitable for
    hashing or comparison.
    """
    sorted_tasks = sorted(tasks, key=lambda t: t.task_id)
    return tuple(t.automated_score for t in sorted_tasks)


def similarity(a: list[CompletedTask], b: list[CompletedTask]) -> float:
    """Compute Jaccard-style similarity between two fingerprints.

    Only considers shared task IDs. Returns 1.0 for identical
    pass/fail patterns, 0.0 for completely opposite patterns.
    """
    scores_a = {t.task_id: t.automated_score for t in a}
    scores_b = {t.task_id: t.automated_score for t in b}

    shared = set(scores_a.keys()) & set(scores_b.keys())
    if not shared:
        return 0.0

    matches = sum(
        1 for tid in shared
        if (scores_a[tid] >= 0.5) == (scores_b[tid] >= 0.5)
    )
    return matches / len(shared)
