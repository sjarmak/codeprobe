"""Cross-validate ground truth across backends after mining.

Compares the file-list ground truth produced by different curator backends
(sourcegraph, ast, grep, ...) on the same task and reports per-pair F1 and
file-level Cohen's kappa. Tasks where any pair disagrees badly (F1 below
``threshold``) are flagged for human review.

Backend variants live alongside the canonical ``ground_truth.json``:

    task_dir/
        ground_truth.json         # canonical (treated as the "default" backend)
        ground_truth_ast.json     # backend = "ast"
        ground_truth_sg.json      # backend = "sg"
        ground_truth_grep.json    # backend = "grep"

Both the org-scale layout (``<task>/ground_truth*.json``) and the
dual/SDLC layout (``<task>/tests/ground_truth*.json``) are supported.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path

from codeprobe.mining.org_scale_oracle import normalize_path, strip_repo_prefix

logger = logging.getLogger(__name__)


_MAX_GT_BYTES = 10 * 1024 * 1024  # 10 MB; matches scoring._load_json_file
_DEFAULT_BACKEND = "default"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendFile:
    """A single ground-truth file attributed to one backend."""

    backend: str
    path: Path


def discover_backend_files(task_dir: Path) -> list[BackendFile]:
    """Find all ground_truth*.json variants for a task.

    Looks in ``task_dir/`` and ``task_dir/tests/``. The canonical
    ``ground_truth.json`` is reported with backend name ``"default"``;
    suffixed variants ``ground_truth_<backend>.json`` use ``<backend>`` as
    the backend label.

    Returns the discovered files sorted by backend name for stable output.
    """
    found: dict[str, Path] = {}
    for parent in (task_dir, task_dir / "tests"):
        if not parent.is_dir():
            continue
        for entry in sorted(parent.iterdir()):
            if not entry.is_file():
                continue
            name = entry.name
            if name == "ground_truth.json":
                # Prefer top-level over tests/ to keep org-scale layout authoritative
                found.setdefault(_DEFAULT_BACKEND, entry)
            elif name.startswith("ground_truth_") and name.endswith(".json"):
                backend = name[len("ground_truth_") : -len(".json")]
                if not backend:
                    continue
                found.setdefault(backend, entry)
    return [BackendFile(b, p) for b, p in sorted(found.items())]


# ---------------------------------------------------------------------------
# File-set extraction
# ---------------------------------------------------------------------------


def _load_ground_truth(path: Path) -> dict | None:
    """Load a ground_truth.json safely (size-bounded, JSON-validated)."""
    try:
        if not path.is_file() or path.stat().st_size > _MAX_GT_BYTES:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("ground_truth not a JSON object: %s", path)
        return None
    return data


def extract_file_set(data: Mapping[str, object]) -> frozenset[str]:
    """Extract the canonical file set from a parsed ground_truth payload.

    Only ``oracle_type == "file_list"`` (and missing — defaults to file_list)
    contribute to file-level cross-validation. Other oracle types
    (count, boolean, structured_retrieval) are returned as an empty set;
    callers should skip them.

    Paths are normalized via :func:`normalize_path` and stripped of the
    ``repo`` prefix when present, matching the oracle's pass-2 matching.
    """
    oracle_type = data.get("oracle_type", "file_list")
    if oracle_type != "file_list":
        return frozenset()

    expected = data.get("expected")
    if not isinstance(expected, list):
        return frozenset()

    repo = data.get("repo", "") if isinstance(data.get("repo"), str) else ""
    return frozenset(
        strip_repo_prefix(normalize_path(str(p)), repo) for p in expected if p
    )


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def _round(value: float, places: int = 4) -> float:
    return round(value, places)


def compute_pair_metrics(
    files_a: frozenset[str], files_b: frozenset[str]
) -> dict[str, float | int]:
    """Symmetric F1/precision/recall between two file sets.

    The pair (a, b) is unordered: precision and recall are reported with
    ``a`` as the reference, but F1 is symmetric. Callers that need a
    directional view should reverse the inputs.
    """
    if not files_a and not files_b:
        return {
            "f1": 1.0, "precision": 1.0, "recall": 1.0,
            "n_a": 0, "n_b": 0, "n_overlap": 0,
        }
    overlap = files_a & files_b
    tp = len(overlap)
    precision = tp / len(files_b) if files_b else 0.0
    recall = tp / len(files_a) if files_a else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    return {
        "f1": _round(f1),
        "precision": _round(precision),
        "recall": _round(recall),
        "n_a": len(files_a),
        "n_b": len(files_b),
        "n_overlap": tp,
    }


def cohens_kappa(
    rater_a: Sequence[frozenset[str]],
    rater_b: Sequence[frozenset[str]],
    universe: frozenset[str],
) -> float:
    """File-level Cohen's kappa across all tasks for a single backend pair.

    Each (task, file) cell is one rating: 1 if the backend included the file,
    0 otherwise. Returns 1.0 when the universe is empty (vacuous agreement).
    """
    if len(rater_a) != len(rater_b):
        raise ValueError("rater_a and rater_b must be parallel sequences")
    if not universe or not rater_a:
        return 1.0

    a = b = c = d = 0
    for set_a, set_b in zip(rater_a, rater_b):
        for f in universe:
            in_a = f in set_a
            in_b = f in set_b
            if in_a and in_b:
                a += 1
            elif in_a:
                b += 1
            elif in_b:
                c += 1
            else:
                d += 1

    n = a + b + c + d
    if n == 0:
        return 1.0
    po = (a + d) / n
    pe = ((a + b) * (a + c) + (c + d) * (b + d)) / (n * n)
    if pe >= 1.0:
        return 1.0
    return _round((po - pe) / (1 - pe))


# ---------------------------------------------------------------------------
# Per-task discovery + comparison
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskBackendData:
    """Loaded backend ground-truth for a single task."""

    task_id: str
    suite: str
    family: str
    backend_files: dict[str, frozenset[str]] = field(default_factory=dict)
    skipped_oracle_types: dict[str, str] = field(default_factory=dict)


def _collect_task(task_dir: Path) -> TaskBackendData | None:
    """Load all backend ground-truth file-sets for one task.

    Returns None when the directory has no ground_truth*.json files at all
    (likely a non-task directory). Returns a TaskBackendData even when only
    one backend is found — the caller decides whether one is enough.
    """
    backends = discover_backend_files(task_dir)
    if not backends:
        return None

    # suite = parent directory name when tasks are organized as
    # <suite>/<task>/, else the literal "tasks" container name. Family pulled
    # from any backend's pattern_used / metadata.json category.
    suite = task_dir.parent.name
    family = ""
    files_by_backend: dict[str, frozenset[str]] = {}
    skipped: dict[str, str] = {}
    for entry in backends:
        data = _load_ground_truth(entry.path)
        if data is None:
            skipped[entry.backend] = "load_failed"
            continue
        oracle_type = str(data.get("oracle_type", "file_list"))
        if oracle_type != "file_list":
            skipped[entry.backend] = f"oracle_type={oracle_type}"
            continue
        if not family:
            pat = data.get("pattern_used")
            if isinstance(pat, str) and pat:
                family = pat
        files_by_backend[entry.backend] = extract_file_set(data)

    if not family:
        meta = task_dir / "metadata.json"
        if meta.is_file():
            try:
                meta_data = json.loads(meta.read_text(encoding="utf-8"))
                cat = (
                    meta_data.get("metadata", {}).get("category")
                    if isinstance(meta_data, dict) else None
                )
                if isinstance(cat, str):
                    family = cat
            except (OSError, json.JSONDecodeError):
                pass

    return TaskBackendData(
        task_id=task_dir.name,
        suite=suite,
        family=family,
        backend_files=files_by_backend,
        skipped_oracle_types=skipped,
    )


def discover_tasks(tasks_dir: Path) -> list[Path]:
    """Find all task directories under ``tasks_dir``.

    A task directory is any direct child that contains either
    ``ground_truth.json`` (org-scale layout) or
    ``tests/ground_truth.json`` (dual/SDLC layout) or any
    ``ground_truth_*.json`` variant. Suite-grouped layouts
    (``tasks_dir/<suite>/<task>/``) are walked one level deeper.
    """
    if not tasks_dir.is_dir():
        return []

    found: list[Path] = []
    for child in sorted(tasks_dir.iterdir()):
        if not child.is_dir():
            continue
        if discover_backend_files(child):
            found.append(child)
            continue
        # Suite-level directory: descend one level
        for grandchild in sorted(child.iterdir()):
            if grandchild.is_dir() and discover_backend_files(grandchild):
                found.append(grandchild)
    return found


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------


def cross_validate(
    tasks_dir: Path,
    *,
    threshold: float = 0.6,
) -> dict:
    """Run cross-validation across all tasks under ``tasks_dir``.

    Returns the report dict. The caller is responsible for writing it to
    disk and choosing the exit code based on ``below_threshold``.
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"threshold must be in [0, 1], got {threshold!r}")

    task_dirs = discover_tasks(tasks_dir)
    tasks: list[TaskBackendData] = []
    for d in task_dirs:
        td = _collect_task(d)
        if td is not None:
            tasks.append(td)

    per_task: list[dict] = []
    flagged: list[dict] = []
    # Collect parallel lists per backend pair for kappa
    pair_universe: dict[tuple[str, str], set[str]] = {}
    pair_sets_a: dict[tuple[str, str], list[frozenset[str]]] = {}
    pair_sets_b: dict[tuple[str, str], list[frozenset[str]]] = {}
    pair_f1s: dict[tuple[str, str], list[float]] = {}

    for task in tasks:
        backends = sorted(task.backend_files.keys())
        if len(backends) < 2:
            per_task.append({
                "task_id": task.task_id,
                "suite": task.suite,
                "family": task.family,
                "backends": backends,
                "pairs": [],
                "min_f1": None,
                "skipped": dict(task.skipped_oracle_types),
                "note": "only one backend with file_list oracle — nothing to compare",
            })
            continue

        pairs: list[dict] = []
        min_f1 = 1.0
        for ba, bb in combinations(backends, 2):
            files_a = task.backend_files[ba]
            files_b = task.backend_files[bb]
            metrics = compute_pair_metrics(files_a, files_b)
            f1 = float(metrics["f1"])
            min_f1 = min(min_f1, f1)
            only_a = sorted(files_a - files_b)
            only_b = sorted(files_b - files_a)
            pairs.append({
                "backend_a": ba,
                "backend_b": bb,
                **metrics,
                f"{ba}_only": only_a,
                f"{bb}_only": only_b,
            })
            key = (ba, bb)
            pair_universe.setdefault(key, set()).update(files_a | files_b)
            pair_sets_a.setdefault(key, []).append(files_a)
            pair_sets_b.setdefault(key, []).append(files_b)
            pair_f1s.setdefault(key, []).append(f1)

        entry = {
            "task_id": task.task_id,
            "suite": task.suite,
            "family": task.family,
            "backends": backends,
            "pairs": pairs,
            "min_f1": _round(min_f1),
            "skipped": dict(task.skipped_oracle_types),
        }
        per_task.append(entry)
        if min_f1 < threshold:
            flagged.append(entry)

    # Aggregate across tasks
    pair_summary: list[dict] = []
    for key, f1s in sorted(pair_f1s.items()):
        ba, bb = key
        kappa = cohens_kappa(
            pair_sets_a[key],
            pair_sets_b[key],
            frozenset(pair_universe[key]),
        )
        pair_summary.append({
            "backend_a": ba,
            "backend_b": bb,
            "n_tasks": len(f1s),
            "mean_f1": _round(sum(f1s) / len(f1s)) if f1s else 0.0,
            "min_f1": _round(min(f1s)) if f1s else 0.0,
            "max_f1": _round(max(f1s)) if f1s else 0.0,
            "cohens_kappa": kappa,
            "kappa_interpretation": _interpret_kappa(kappa),
            "n_below_threshold": sum(1 for f in f1s if f < threshold),
        })

    # Per-suite/family aggregates
    suite_summary: dict[str, dict] = {}
    family_summary: dict[str, dict] = {}
    for entry in per_task:
        if entry.get("min_f1") is None:
            continue
        for bucket, key in (
            (suite_summary, entry["suite"]),
            (family_summary, entry.get("family") or ""),
        ):
            if not key:
                continue
            slot = bucket.setdefault(key, {"n_tasks": 0, "min_f1s": []})
            slot["n_tasks"] += 1
            slot["min_f1s"].append(entry["min_f1"])

    def _finalize(buckets: dict[str, dict]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for key, slot in sorted(buckets.items()):
            f1s = slot["min_f1s"]
            out[key] = {
                "n_tasks": slot["n_tasks"],
                "mean_min_f1": _round(sum(f1s) / len(f1s)) if f1s else 0.0,
                "n_below_threshold": sum(1 for f in f1s if f < threshold),
            }
        return out

    n_compared = sum(
        1 for e in per_task if e.get("min_f1") is not None
    )

    return {
        "summary": {
            "tasks_dir": str(tasks_dir),
            "threshold": threshold,
            "total_tasks": len(per_task),
            "tasks_compared": n_compared,
            "tasks_with_single_backend": len(per_task) - n_compared,
            "tasks_above_threshold": n_compared - len(flagged),
            "tasks_below_threshold": len(flagged),
        },
        "pair_summary": pair_summary,
        "per_suite": _finalize(suite_summary),
        "per_family": _finalize(family_summary),
        "flagged_tasks": [t["task_id"] for t in flagged],
        "per_task": per_task,
    }


def _interpret_kappa(k: float) -> str:
    """Landis & Koch (1977) interpretation of Cohen's kappa."""
    if k < 0:
        return "poor"
    if k <= 0.20:
        return "slight"
    if k <= 0.40:
        return "fair"
    if k <= 0.60:
        return "moderate"
    if k <= 0.80:
        return "substantial"
    return "almost_perfect"


def write_report(report: dict, tasks_dir: Path) -> Path:
    """Write the cross-validation report to ``<tasks_dir>/.codeprobe/...``."""
    out_dir = tasks_dir / ".codeprobe"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "cross_validation_report.json"
    out_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out_path
