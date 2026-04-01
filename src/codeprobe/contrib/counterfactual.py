"""Counterfactual analysis — find tasks where configs diverge.

Identifies tasks where one config passes and the other fails,
revealing the specific strengths and weaknesses of each setup.
"""

from __future__ import annotations

from dataclasses import dataclass

from codeprobe.contrib._shared import PASS_THRESHOLD as _PASS_THRESHOLD
from codeprobe.models.experiment import ConfigResults


@dataclass(frozen=True)
class DivergentTask:
    """A task where two configs produced different pass/fail outcomes."""

    task_id: str
    config_a: str
    score_a: float
    config_b: str
    score_b: float


def find_divergent_tasks(
    a: ConfigResults,
    b: ConfigResults,
    threshold: float = _PASS_THRESHOLD,
) -> list[DivergentTask]:
    """Find tasks where one config passes and the other fails.

    Args:
        a: First config results.
        b: Second config results.
        threshold: Score threshold for pass/fail classification.

    Returns:
        List of divergent tasks, sorted by task ID.
    """
    scores_a = {t.task_id: t.automated_score for t in a.completed}
    scores_b = {t.task_id: t.automated_score for t in b.completed}

    shared = sorted(set(scores_a.keys()) & set(scores_b.keys()))
    divergent: list[DivergentTask] = []

    for tid in shared:
        sa, sb = scores_a[tid], scores_b[tid]
        pass_a = sa >= threshold
        pass_b = sb >= threshold
        if pass_a != pass_b:
            divergent.append(DivergentTask(tid, a.config, sa, b.config, sb))

    return divergent
