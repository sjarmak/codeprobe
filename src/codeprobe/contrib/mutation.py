"""Mutation / sensitivity analysis for experiment robustness.

Randomly flips task scores and measures how much the aggregate
pass rate changes, revealing whether results are fragile.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from codeprobe.contrib._shared import PASS_THRESHOLD as _PASS_THRESHOLD
from codeprobe.models.experiment import CompletedTask


@dataclass(frozen=True)
class SensitivityResult:
    """Summary of mutation sensitivity analysis."""

    mean_pass_rate: float
    std_pass_rate: float
    original_pass_rate: float
    iterations: int
    flip_fraction: float


def sensitivity_analysis(
    tasks: list[CompletedTask],
    flip_fraction: float = 0.1,
    iterations: int = 100,
    seed: int | None = None,
) -> SensitivityResult:
    """Measure sensitivity of pass rate to random score flips.

    Args:
        tasks: Completed task results.
        flip_fraction: Fraction of tasks to flip per iteration (0.0-1.0).
        iterations: Number of Monte Carlo iterations.
        seed: Random seed for reproducibility.

    Returns:
        SensitivityResult with mean/std of mutated pass rates.
    """
    rng = random.Random(seed)
    n = len(tasks)
    if n == 0:
        return SensitivityResult(0.0, 0.0, 0.0, iterations, flip_fraction)

    scores = [t.automated_score for t in tasks]
    original_rate = sum(1.0 for s in scores if s >= _PASS_THRESHOLD) / n
    flip_count = max(1, int(n * flip_fraction))

    rates: list[float] = []
    for _ in range(iterations):
        mutated = list(scores)
        indices = rng.sample(range(n), min(flip_count, n))
        for idx in indices:
            mutated[idx] = 0.0 if mutated[idx] >= _PASS_THRESHOLD else 1.0
        rate = sum(1.0 for s in mutated if s >= _PASS_THRESHOLD) / n
        rates.append(rate)

    mean = sum(rates) / len(rates)
    variance = sum((r - mean) ** 2 for r in rates) / len(rates)
    std = variance**0.5

    return SensitivityResult(mean, std, original_rate, iterations, flip_fraction)
