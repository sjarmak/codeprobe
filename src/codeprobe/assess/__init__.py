"""Assess — evaluate a codebase's benchmarking potential."""

from __future__ import annotations

from codeprobe.assess.heuristics import (
    RUBRIC_V1,
    AssessmentScore,
    DimensionScore,
    RepoHeuristics,
    assess_repo,
    gather_heuristics,
    score_repo,
    score_repo_heuristic,
    score_repo_with_model,
)
from codeprobe.assess.oracle_diff import (
    CheckOutcome,
    FlavorResult,
    flavor_b_from_csb_manifest,
    flavor_b_score_correlation,
    flavor_c_e2e_divergence,
)

__all__ = [
    "AssessmentScore",
    "CheckOutcome",
    "DimensionScore",
    "FlavorResult",
    "RUBRIC_V1",
    "RepoHeuristics",
    "assess_repo",
    "flavor_b_from_csb_manifest",
    "flavor_b_score_correlation",
    "flavor_c_e2e_divergence",
    "gather_heuristics",
    "score_repo",
    "score_repo_heuristic",
    "score_repo_with_model",
]
