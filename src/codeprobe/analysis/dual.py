"""Dual-scoring composite helpers (direct vs artifact).

A task is "dual-scored" when its ``scoring_details`` dict carries at least one
of ``score_direct`` / ``score_artifact`` / ``passed_direct`` / ``passed_artifact``.
``dual_composite`` combines the two legs into a single float under one of the
supported strategies so downstream stats/reporting code can treat dual tasks
uniformly with single-leg tasks.

This module is the canonical home for the dual-scoring predicates and format
helpers shared across ``analysis.stats``, ``analysis.report``, and the CLI
listeners. The ``stats`` import is deferred to function bodies to avoid a
module-level circular import (``stats`` imports ``has_dual_scoring`` from here).
"""

from __future__ import annotations

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


def _strict_bool(value: object) -> bool | None:
    """Return a bool only for actual bool or recognizable serialized forms.

    Returns ``None`` when *value* is absent or of an unexpected type, so
    callers can fall back to a score threshold. This guards against the
    ``bool("False") is True`` pitfall for JSON-round-tripped checkpoints.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"true", "1", "yes", "y"}:
            return True
        if s in {"false", "0", "no", "n"}:
            return False
        return None
    return None


def resolve_leg_pass(task: CompletedTask) -> tuple[bool, bool]:
    """Return ``(direct_pass, artifact_pass)`` booleans for a dual task.

    Prefers explicit ``passed_direct`` / ``passed_artifact`` bools from the
    scoring_details dict; otherwise thresholds the raw score on
    :data:`codeprobe.analysis.stats.PASS_THRESHOLD`.
    """
    from codeprobe.analysis.stats import PASS_THRESHOLD

    details = task.scoring_details or {}
    try:
        direct_score = float(details.get("score_direct", 0.0))
    except (TypeError, ValueError):
        direct_score = 0.0
    try:
        artifact_score = float(details.get("score_artifact", 0.0))
    except (TypeError, ValueError):
        artifact_score = 0.0
    direct_raw = _strict_bool(details.get("passed_direct"))
    artifact_raw = _strict_bool(details.get("passed_artifact"))
    direct_pass = (
        direct_raw if direct_raw is not None else direct_score >= PASS_THRESHOLD
    )
    artifact_pass = (
        artifact_raw if artifact_raw is not None else artifact_score >= PASS_THRESHOLD
    )
    return direct_pass, artifact_pass


def format_dual_suffix(scoring_details: dict | None) -> str:
    """Return a ``" (code:… artifact:…)"`` suffix when dual scoring is present.

    Returns an empty string when *scoring_details* is None or does not contain
    both ``score_direct`` and ``score_artifact`` fields. Shared by the plain
    text and rich CLI listeners so both render identical per-task output.
    """
    if not scoring_details:
        return ""
    if "score_direct" not in scoring_details or "score_artifact" not in scoring_details:
        return ""
    code_str = "PASS" if scoring_details.get("passed_direct") else "FAIL"
    artifact_score = scoring_details["score_artifact"]
    try:
        artifact_str = f"{float(artifact_score):.2f}"
    except (TypeError, ValueError):
        artifact_str = str(artifact_score)
    return f" (code:{code_str} artifact:{artifact_str})"


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
        direct_pass, artifact_pass = resolve_leg_pass(task)
        return 1.0 if (direct_pass and artifact_pass) else 0.0

    raise ValueError(
        f"unknown dual_composite strategy: {strategy!r} "
        "(expected 'min', 'mean', or 'gate')"
    )
