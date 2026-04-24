"""Oracle comparison for org-scale tasks.

Supports three oracle types:
- **file_list** (default): frozenset F1/precision/recall/jaccard scoring
- **count**: exact integer match with optional ±tolerance
- **boolean**: normalized true/false comparison

All file_list comparison uses frozenset (not list) to prevent duplicate inflation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def normalize_path(path: str) -> str:
    """Normalize a file path for oracle comparison.

    Strips common prefixes, normalizes separators, and removes leading dots.
    Loops until stable to handle combined prefixes like ``/tmp/./pkg/foo.go``.
    """
    p = path.replace("\\", "/").strip()
    _PREFIXES = ("./", "/workspace/", "/tmp/", "/app/")  # noqa: N806
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


def strip_repo_prefix(path: str, repo: str) -> str:
    """Strip leading segments up to and including ``<repo>/`` when present.

    Handles agent answers written with a repo-name prefix — either bare
    (``kubernetes/pkg/foo.go``) or embedded in an absolute path
    (``/home/user/kubernetes/pkg/foo.go``, ``github.com/k/kubernetes/pkg/foo.go``).
    Returns ``path`` unchanged when ``repo`` is empty or the segment is
    absent; safe to apply to oracle paths that don't contain the repo name.
    """
    if not repo:
        return path
    seg = "/" + repo + "/"
    idx = path.rfind(seg)
    if idx >= 0:
        return path[idx + len(seg) :]
    prefix = repo + "/"
    if path.startswith(prefix):
        return path[len(prefix) :]
    return path


def _read_answer_raw(task_dir: Path) -> str | None:
    """Read answer.txt and return raw text, or None on failure."""
    answer_file = task_dir / "answer.txt"
    if not answer_file.exists():
        logger.warning("No answer.txt found in %s", task_dir)
        return None
    try:
        return answer_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Failed to read answer.txt: %s", exc)
        return None


def extract_answer(
    task_dir: Path,
    oracle_type: str = "file_list",
) -> list[str] | int | bool | None:
    """Extract the agent's answer from answer.txt in the task directory.

    Returns:
        - ``list[str]``: normalized file paths for ``file_list``
        - ``int``: parsed integer for ``count``
        - ``bool``: normalized boolean for ``boolean``
        - ``None``: on missing/unreadable answer.txt or parse failure
    """
    raw = _read_answer_raw(task_dir)
    if raw is None:
        return [] if oracle_type == "file_list" else None

    if oracle_type == "count":
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                try:
                    return int(line)
                except ValueError:
                    logger.warning("Cannot parse count answer: %r", line)
                    return None
        return None

    if oracle_type == "boolean":
        for line in raw.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return _normalize_bool(line)
        return None

    # file_list (default)
    paths: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            normalized = normalize_path(line)
            if normalized:
                paths.append(normalized)
    return paths


_TRUE_VALS = frozenset({"true", "yes", "1"})
_FALSE_VALS = frozenset({"false", "no", "0"})


def _normalize_bool(value: str) -> bool | None:
    """Normalize a string to a boolean, or None if unrecognized."""
    v = value.strip().lower()
    if v in _TRUE_VALS:
        return True
    if v in _FALSE_VALS:
        return False
    logger.warning("Cannot normalize boolean value: %r", value)
    return None


def oracle_check(
    task_dir: Path,
    *,
    metric: str = "auto",
) -> dict[str, float | str]:
    """Compare agent answer against ground truth.

    Dispatches to type-specific checkers based on ``oracle_type`` in
    ``ground_truth.json`` (defaults to ``"file_list"``).

    Args:
        task_dir: Task directory containing answer.txt and ground_truth.json.
        metric: Primary metric (only used for file_list type). The default
            ``"auto"`` picks weighted F1 when ``oracle_tiers`` is present
            in ground_truth.json and plain F1 otherwise, matching CSB's
            ``_get_primary_score``. Pass ``"f1"``, ``"weighted_f1"``,
            ``"precision"``, ``"recall"``, or ``"jaccard"`` to force.

    Returns:
        Dict with at least ``score`` and ``error`` keys.
    """
    gt_path = task_dir / "ground_truth.json"
    if not gt_path.exists():
        return {"score": 0.0, "error": f"Missing {gt_path}"}

    try:
        gt_data = json.loads(gt_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"score": 0.0, "error": f"Invalid ground_truth.json: {exc}"}

    oracle_type = gt_data.get("oracle_type", "file_list")

    if oracle_type == "count":
        return _check_count(task_dir, gt_data)
    if oracle_type == "boolean":
        return _check_boolean(task_dir, gt_data)
    if oracle_type == "file_list":
        return _check_file_list(task_dir, gt_data, metric=metric)

    return {"score": 0.0, "error": f"Unknown oracle_type: {oracle_type!r}"}


def _check_file_list(
    task_dir: Path,
    gt_data: dict,
    *,
    metric: str = "auto",
) -> dict[str, float | str]:
    """File-list oracle: frozenset F1/precision/recall/jaccard scoring.

    When ``metric="auto"`` (default), the primary score is weighted F1
    if ``oracle_tiers`` is present in ground_truth.json, else plain F1 —
    matching CSB's ``_get_primary_score`` behavior. Callers can force a
    specific metric ("f1", "weighted_f1", "precision", "recall", "jaccard").

    Path matching is two-pass: exact normalized, then repo-prefix stripped
    (uses ``repo`` from ground_truth.json when present) so agents that
    report paths as ``<repo>/<path>`` still match oracle ``<path>`` entries.
    """
    expected_raw = gt_data.get("expected", [])
    if not isinstance(expected_raw, list):
        return {"score": 0.0, "error": "ground_truth.json 'expected' is not a list"}

    repo = gt_data.get("repo", "") or ""
    oracle_tiers_raw: dict[str, str] = gt_data.get("oracle_tiers", {}) or {}
    has_tiers = bool(oracle_tiers_raw)

    def _canon(p: str) -> str:
        return strip_repo_prefix(normalize_path(p), repo)

    expected: frozenset[str] = frozenset(_canon(p) for p in expected_raw if p)
    if not expected:
        return {"score": 0.0, "error": "Empty ground truth"}

    # Re-key tier map to the canonical (normalized+stripped) paths so tier
    # lookups survive the same transformation applied to expected/agent.
    oracle_tiers: dict[str, str] = {
        _canon(k): v for k, v in oracle_tiers_raw.items()
    }

    agent_paths = extract_answer(task_dir, oracle_type="file_list")
    agent_answer: frozenset[str] = frozenset(
        strip_repo_prefix(p, repo) for p in (agent_paths or [])  # type: ignore[union-attr]
    )
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

    # Weighted F1 — computed whenever tiers exist OR the caller explicitly
    # asked for weighted_f1 (in which case missing tiers default every file
    # to 'required' and the result degenerates to plain F1). Callers always
    # get weighted_f1 in the payload for inspection; only the primary
    # ``score`` field depends on metric selection.
    weighted_metrics: dict[str, float] = {}
    if has_tiers or metric == "weighted_f1":
        weighted_metrics = _weighted_f1(expected, agent_answer, oracle_tiers)

    # Auto-select primary metric: weighted_f1 when tiers present, else f1.
    effective_metric = metric
    if metric == "auto":
        effective_metric = "weighted_f1" if has_tiers else "f1"

    metric_map: dict[str, float] = {
        "f1": f1,
        "recall": recall,
        "precision": precision,
        "jaccard": jaccard,
        "weighted_f1": weighted_metrics.get("weighted_f1", f1),
    }

    result: dict[str, float | str | int] = {
        "score": round(metric_map.get(effective_metric, f1), 4),
        "metric": effective_metric,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "jaccard": round(jaccard, 4),
        "intersection_size": intersection_size,
        "expected_size": len(expected),
        "answer_size": len(agent_answer),
        "error": "",
    }

    if weighted_metrics:
        result["weighted_f1"] = weighted_metrics["weighted_f1"]
        result["weighted_recall"] = weighted_metrics["weighted_recall"]

    return result


_TIER_WEIGHTS: dict[str, float] = {
    "required": 2.0,
    "supplementary": 1.0,
    "context": 0.5,
}


def _weighted_f1(
    expected: frozenset[str],
    agent_answer: frozenset[str],
    oracle_tiers: dict[str, str],
) -> dict[str, float]:
    """Compute weighted F1 where recall weights files by tier.

    Tier weights: required=2.0, supplementary=1.0, context=0.5.
    Missing tiers default to 'required' (backward compatible).
    Precision is unweighted (standard).
    """
    intersection = expected & agent_answer

    # Weighted recall: sum(weight[tier] for matched) / sum(weight[tier] for expected)
    weighted_hit = sum(
        _TIER_WEIGHTS.get(oracle_tiers.get(f, "required"), 2.0) for f in intersection
    )
    weighted_total = sum(
        _TIER_WEIGHTS.get(oracle_tiers.get(f, "required"), 2.0) for f in expected
    )
    weighted_recall = weighted_hit / weighted_total if weighted_total > 0 else 0.0

    # Standard precision (unweighted)
    precision = len(intersection) / len(agent_answer) if agent_answer else 0.0

    # Weighted F1
    denom = precision + weighted_recall
    wf1 = 2.0 * precision * weighted_recall / denom if denom > 0 else 0.0

    return {
        "weighted_recall": round(weighted_recall, 4),
        "weighted_f1": round(wf1, 4),
    }


def _check_count(
    task_dir: Path,
    gt_data: dict,
) -> dict[str, Any]:
    """Count oracle: exact integer match with optional ±tolerance."""
    expected = gt_data.get("expected")
    if not isinstance(expected, int):
        return {
            "score": 0.0,
            "error": "ground_truth.json 'expected' is not an int for count type",
        }

    tolerance = gt_data.get("tolerance", 0)
    if not isinstance(tolerance, int) or tolerance < 0:
        return {"score": 0.0, "error": f"Invalid tolerance: {tolerance!r}"}

    agent_val = extract_answer(task_dir, oracle_type="count")
    if agent_val is None:
        return {
            "score": 0.0,
            "expected": expected,
            "agent_answer": None,
            "error": "Empty or unparseable agent answer for count type",
        }

    match = abs(agent_val - expected) <= tolerance  # type: ignore[operator]
    return {
        "score": 1.0 if match else 0.0,
        "expected": expected,
        "agent_answer": agent_val,
        "tolerance": tolerance,
        "error": "",
    }


def _check_boolean(
    task_dir: Path,
    gt_data: dict,
) -> dict[str, Any]:
    """Boolean oracle: normalized true/false comparison."""
    expected = gt_data.get("expected")
    if not isinstance(expected, bool):
        return {
            "score": 0.0,
            "error": "ground_truth.json 'expected' is not a bool for boolean type",
        }

    agent_val = extract_answer(task_dir, oracle_type="boolean")
    if agent_val is None:
        return {
            "score": 0.0,
            "expected": expected,
            "agent_answer": None,
            "error": "Empty or unparseable agent answer for boolean type",
        }

    return {
        "score": 1.0 if agent_val == expected else 0.0,
        "expected": expected,
        "agent_answer": agent_val,
        "error": "",
    }
