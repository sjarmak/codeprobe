"""Validated arithmetic for dual-scoring policies."""

from __future__ import annotations

import math
from dataclasses import dataclass

VALID_DUAL_SCORING_POLICIES = frozenset({"", "min", "mean", "gate", "weighted"})
DUAL_WEIGHT_SUM_TOLERANCE = 1e-6


@dataclass(frozen=True)
class DualPolicy:
    """A validated dual-scoring policy and its normalized weights."""

    name: str
    weight_direct: float = 0.5
    weight_artifact: float = 0.5


def _parse_weight(raw: object, *, name: str, default: float) -> float:
    if raw is None:
        return default
    try:
        value = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} has invalid value {raw!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {raw!r}")
    if value < 0.0 or value > 1.0:
        raise ValueError(f"{name} must be in [0, 1], got {value}")
    return value


def resolve_dual_policy(
    raw_policy: object,
    raw_weight_direct: object = None,
    raw_weight_artifact: object = None,
) -> DualPolicy:
    """Validate policy metadata and return its mechanical scoring inputs."""
    if not isinstance(raw_policy, str) or raw_policy not in VALID_DUAL_SCORING_POLICIES:
        raise ValueError(
            f"scoring_policy {raw_policy!r} is invalid; expected one of "
            f"{sorted(VALID_DUAL_SCORING_POLICIES)!r}"
        )
    if raw_policy != "weighted":
        return DualPolicy(name=raw_policy)

    weight_direct = _parse_weight(
        raw_weight_direct,
        name="weight_direct",
        default=0.5,
    )
    weight_artifact = _parse_weight(
        raw_weight_artifact,
        name="weight_artifact",
        default=0.5,
    )
    total = weight_direct + weight_artifact
    if abs(total - 1.0) > DUAL_WEIGHT_SUM_TOLERANCE:
        raise ValueError(
            "weight sum must equal 1.0 "
            f"(weight_direct={weight_direct}, "
            f"weight_artifact={weight_artifact}, sum={total})"
        )
    return DualPolicy(
        name=raw_policy,
        weight_direct=weight_direct,
        weight_artifact=weight_artifact,
    )


def compose_dual_score(
    policy: DualPolicy,
    *,
    score_direct: float,
    score_artifact: float,
    passed_direct: bool,
    passed_artifact: bool,
) -> float:
    """Apply a validated policy and return a bounded composite score."""
    if not math.isfinite(score_direct) or not math.isfinite(score_artifact):
        raise ValueError("dual leg scores must be finite")

    if policy.name in {"", "min"}:
        composite = (
            score_direct
            if policy.name == ""
            else min(score_direct, score_artifact)
        )
    elif policy.name == "mean":
        composite = (score_direct + score_artifact) / 2.0
    elif policy.name == "gate":
        composite = 1.0 if passed_direct and passed_artifact else 0.0
    elif policy.name == "weighted":
        composite = (
            policy.weight_direct * score_direct
            + policy.weight_artifact * score_artifact
        )
    else:
        raise ValueError(f"unsupported dual policy: {policy.name!r}")
    return max(0.0, min(1.0, composite))


__all__ = [
    "DUAL_WEIGHT_SUM_TOLERANCE",
    "VALID_DUAL_SCORING_POLICIES",
    "DualPolicy",
    "compose_dual_score",
    "resolve_dual_policy",
]
