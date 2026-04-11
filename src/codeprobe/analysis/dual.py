"""Dual-scoring composite helpers (direct vs artifact).

A task is "dual-scored" when its ``scoring_details`` dict carries at least one
of ``score_direct`` / ``score_artifact`` / ``passed_direct`` / ``passed_artifact``.
``dual_composite`` combines the two legs into a single float under one of the
supported strategies so downstream stats/reporting code can treat dual tasks
uniformly with single-leg tasks.

When a task has no dual scoring details, ``dual_composite`` transparently
falls back to ``task.automated_score`` so callers can use it unconditionally.
"""

from __future__ import annotations

from codeprobe.analysis.stats import PASS_THRESHOLD
from codeprobe.models.experiment import CompletedTask, DualScoringDetails

_DUAL_KEYS = (
    "score_direct",
    "score_artifact",
    "passed_direct",
    "passed_artifact",
)


def has_dual_scoring(task: CompletedTask) -> bool:
    """Return True if *task* carries dual scoring details."""
    details = task.scoring_details or {}
    return any(key in details for key in _DUAL_KEYS)


def dual_composite(task: CompletedTask, strategy: str = "min") -> float:
    """Composite a dual-scored task's legs into a single score.

    Strategies:
      * ``'min'``  — ``min(score_direct, score_artifact)``.
      * ``'mean'`` — ``(score_direct + score_artifact) / 2``.
      * ``'gate'`` — ``1.0`` if both legs pass (using ``passed_*`` flags, or
        falling back to ``score_* >= PASS_THRESHOLD``), else ``0.0``.

    If *task* has no dual scoring details, returns ``task.automated_score``
    as a passthrough so callers can apply this uniformly.

    Raises:
        ValueError: if *strategy* is not one of the supported values.
    """
    if not has_dual_scoring(task):
        return task.automated_score

    details = DualScoringDetails.from_dict(task.scoring_details)

    if strategy == "min":
        return min(details.score_direct, details.score_artifact)
    if strategy == "mean":
        return (details.score_direct + details.score_artifact) / 2.0
    if strategy == "gate":
        # Prefer explicit passed_* flags when they were supplied in the raw
        # scoring_details dict; otherwise threshold the raw scores.
        raw = task.scoring_details or {}
        if "passed_direct" in raw:
            direct_pass = bool(raw["passed_direct"])
        else:
            direct_pass = details.score_direct >= PASS_THRESHOLD
        if "passed_artifact" in raw:
            artifact_pass = bool(raw["passed_artifact"])
        else:
            artifact_pass = details.score_artifact >= PASS_THRESHOLD
        return 1.0 if (direct_pass and artifact_pass) else 0.0

    raise ValueError(
        f"unknown dual_composite strategy: {strategy!r} "
        "(expected 'min', 'mean', or 'gate')"
    )
