"""Sequential Probability Ratio Test for early stopping.

Determines whether an agent's pass rate significantly exceeds a baseline
threshold, allowing experiments to stop early when evidence is conclusive.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SPRTResult:
    """Outcome of a sequential probability ratio test."""

    decision: str  # "accept", "reject", or "continue"
    log_likelihood_ratio: float
    samples_used: int
    upper_bound: float
    lower_bound: float


def sprt_test(
    scores: list[float],
    theta_0: float = 0.5,
    theta_1: float = 0.7,
    alpha: float = 0.05,
    beta: float = 0.2,
) -> SPRTResult:
    """Run SPRT on a sequence of binary-ish scores.

    Args:
        scores: Task scores (treated as Bernoulli: >= 0.5 is a pass).
        theta_0: Null hypothesis pass rate.
        theta_1: Alternative hypothesis pass rate (must be > theta_0).
        alpha: Type I error rate (false positive).
        beta: Type II error rate (false negative).

    Returns:
        SPRTResult with decision and diagnostic values.
    """
    upper = math.log((1 - beta) / alpha)
    lower = math.log(beta / (1 - alpha))

    llr = 0.0
    for i, s in enumerate(scores, 1):
        p = 1.0 if s >= 0.5 else 0.0
        if p == 1.0:
            llr += math.log(theta_1 / theta_0) if theta_0 > 0 else float("inf")
        else:
            denom_0 = 1 - theta_0 if theta_0 < 1 else 1e-12
            denom_1 = 1 - theta_1 if theta_1 < 1 else 1e-12
            llr += math.log(denom_1 / denom_0)

        if llr >= upper:
            return SPRTResult("accept", llr, i, upper, lower)
        if llr <= lower:
            return SPRTResult("reject", llr, i, upper, lower)

    return SPRTResult("continue", llr, len(scores), upper, lower)
