"""MCP delta validation for org-scale task families.

Runs a grep-only baseline scorer against sample tasks and flags families
where grep alone nearly solves the task (no MCP advantage).

ZFC compliant: pure arithmetic comparison, no semantic judgment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from codeprobe.mining.org_scale_families import TaskFamily
from codeprobe.mining.org_scale_oracle import normalize_path
from codeprobe.mining.org_scale_scanner import scan_repo_for_family
from codeprobe.models.task import Task

logger = logging.getLogger(__name__)

_BASELINE_THRESHOLD = 0.95


@dataclass(frozen=True)
class DeltaResult:
    """Result of validating one family's MCP delta.

    Attributes:
        family_name: The task family identifier.
        grep_f1: Average F1 of the grep-only baseline across sample tasks.
        is_baseline_only: True when grep alone nearly solves it (grep_f1 >= 0.95).
        sample_count: Number of sample tasks evaluated.
        details: Human-readable summary of the validation.
    """

    family_name: str
    grep_f1: float
    is_baseline_only: bool
    sample_count: int
    details: str


def _compute_f1(predicted: frozenset[str], expected: frozenset[str]) -> float:
    """Compute F1 score between predicted and expected file sets."""
    if not expected:
        return 1.0 if not predicted else 0.0
    if not predicted:
        return 0.0
    intersection = len(predicted & expected)
    precision = intersection / len(predicted)
    recall = intersection / len(expected)
    if precision + recall == 0.0:
        return 0.0
    return 2.0 * precision * recall / (precision + recall)


def validate_family_delta(
    family: TaskFamily,
    sample_tasks: list[Task],
    repo_paths: list[Path],
) -> DeltaResult:
    """Validate whether a task family differentiates MCP from grep-only.

    For each sample task, runs the family's regex patterns against the
    corresponding repo to get grep-matched files, then compares against
    the task's ground truth (oracle_answer) via F1 scoring.

    Args:
        family: The task family to validate.
        sample_tasks: Tasks with ground truth in verification.oracle_answer.
        repo_paths: Repo paths corresponding 1:1 to sample_tasks.

    Returns:
        DeltaResult with grep_f1, is_baseline_only flag, and details.
    """
    if not sample_tasks:
        return DeltaResult(
            family_name=family.name,
            grep_f1=0.0,
            is_baseline_only=False,
            sample_count=0,
            details="No sample tasks provided",
        )

    f1_scores: list[float] = []
    task_details: list[str] = []

    for task, repo_path in zip(sample_tasks, repo_paths):
        expected_raw = task.verification.oracle_answer
        expected = frozenset(normalize_path(p) for p in expected_raw if p)

        if not expected:
            task_details.append(f"{task.id}: skipped (empty ground truth)")
            continue

        scan_result = scan_repo_for_family([repo_path], family)
        grep_files = frozenset(normalize_path(f) for f in scan_result.matched_files)

        f1 = _compute_f1(grep_files, expected)
        f1_scores.append(f1)
        task_details.append(
            f"{task.id}: f1={f1:.3f} "
            f"(grep={len(grep_files)}, truth={len(expected)}, "
            f"overlap={len(grep_files & expected)})"
        )

    avg_f1 = sum(f1_scores) / len(f1_scores) if f1_scores else 0.0
    is_baseline_only = avg_f1 >= _BASELINE_THRESHOLD

    details = (
        f"avg_f1={avg_f1:.3f}, threshold={_BASELINE_THRESHOLD}, "
        f"baseline_only={is_baseline_only}\n" + "\n".join(task_details)
    )

    return DeltaResult(
        family_name=family.name,
        grep_f1=round(avg_f1, 4),
        is_baseline_only=is_baseline_only,
        sample_count=len(f1_scores),
        details=details,
    )


def validate_families(
    families: list[TaskFamily],
    tasks: list[list[Task]],
    repo_paths: list[list[Path]],
) -> list[DeltaResult]:
    """Validate multiple families for MCP delta.

    Args:
        families: Task families to validate.
        tasks: Per-family lists of sample tasks (parallel to families).
        repo_paths: Per-family lists of repo paths (parallel to tasks).

    Returns:
        List of DeltaResult, one per family.
    """
    results: list[DeltaResult] = []
    for family, family_tasks, family_repos in zip(families, tasks, repo_paths):
        result = validate_family_delta(family, family_tasks, family_repos)
        logger.info(
            "Family %s: grep_f1=%.3f baseline_only=%s",
            family.name,
            result.grep_f1,
            result.is_baseline_only,
        )
        results.append(result)
    return results
