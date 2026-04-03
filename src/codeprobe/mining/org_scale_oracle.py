"""Oracle comparison for org-scale tasks.

Compares agent answers (file lists) against structurally-computed ground
truth using F1/recall/precision/jaccard scoring.

All comparison uses frozenset (not list) to prevent duplicate inflation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def normalize_path(path: str) -> str:
    """Normalize a file path for oracle comparison.

    Strips common prefixes, normalizes separators, and removes leading dots.
    Loops until stable to handle combined prefixes like ``/tmp/./pkg/foo.go``.
    """
    p = path.replace("\\", "/").strip()
    _PREFIXES = ("./", "/workspace/", "/tmp/", "/app/")
    changed = True
    while changed:
        changed = False
        for prefix in _PREFIXES:
            if p.startswith(prefix):
                p = p[len(prefix) :]
                changed = True
        if p.startswith("/"):
            p = p.lstrip("/")
            changed = True
    return p


def extract_answer(task_dir: Path) -> list[str]:
    """Extract the agent's answer from answer.txt in the task directory.

    Returns a list of normalized file paths (blank lines and comments skipped).
    """
    answer_file = task_dir / "answer.txt"
    if not answer_file.exists():
        logger.warning("No answer.txt found in %s", task_dir)
        return []

    try:
        raw = answer_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Failed to read answer.txt: %s", exc)
        return []

    paths: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            normalized = normalize_path(line)
            if normalized:
                paths.append(normalized)

    return paths


def oracle_check(
    task_dir: Path,
    *,
    metric: str = "f1",
) -> dict[str, float | str]:
    """Compare agent answer against ground truth.

    Args:
        task_dir: Task directory containing answer.txt and ground_truth.json.
        metric: Primary metric: ``"f1"``, ``"recall"``, ``"precision"``, ``"jaccard"``.

    Returns:
        Dict with ``score``, ``precision``, ``recall``, ``f1``, ``jaccard``,
        raw counts, and ``error`` (empty string if no error).
    """
    gt_path = task_dir / "ground_truth.json"
    if not gt_path.exists():
        return {"score": 0.0, "error": f"Missing {gt_path}"}

    try:
        gt_data = json.loads(gt_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"score": 0.0, "error": f"Invalid ground_truth.json: {exc}"}

    expected_raw = gt_data.get("expected", [])
    if not isinstance(expected_raw, list):
        return {"score": 0.0, "error": "ground_truth.json 'expected' is not a list"}

    expected: frozenset[str] = frozenset(normalize_path(p) for p in expected_raw if p)
    if not expected:
        return {"score": 0.0, "error": "Empty ground truth"}

    agent_answer: frozenset[str] = frozenset(extract_answer(task_dir))
    if not agent_answer:
        return {
            "score": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "jaccard": 0.0,
            "error": "Empty agent answer (no answer.txt or no valid paths)",
        }

    # Compute metrics using frozensets (premortem P0: no duplicate inflation)
    intersection_size = len(expected & agent_answer)
    precision = intersection_size / len(agent_answer)
    recall = intersection_size / len(expected)
    f1 = (
        2.0 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    union_size = len(expected | agent_answer)
    jaccard = intersection_size / union_size if union_size else 0.0

    # Invariant check — return error instead of crashing
    for name, val in (("f1", f1), ("precision", precision), ("recall", recall)):
        if not (0.0 <= val <= 1.0):
            return {"score": 0.0, "error": f"{name} out of bounds: {val}"}

    metric_map = {
        "f1": f1,
        "recall": recall,
        "precision": precision,
        "jaccard": jaccard,
    }

    return {
        "score": round(metric_map.get(metric, f1), 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "jaccard": round(jaccard, 4),
        "intersection_size": intersection_size,
        "expected_size": len(expected),
        "answer_size": len(agent_answer),
        "error": "",
    }
