"""Per-task confidence scoring for mined tasks.

Combines four mechanical signals into a single ``ConfidenceScore``:

* **Cross-source agreement** — from the cross-validation report produced by
  ``codeprobe mine-cross-validate``. Tasks flagged as low-agreement get a
  low sub-score; tasks with no comparable backends get a neutral score.
* **Ground-truth size sanity** — extreme small (< 1) or extreme large
  (> 100) ground-truth file sets are suspicious. The sweet spot of 3-50
  files gets full credit.
* **Instruction quality** — auto-truncation markers, very short bodies,
  or missing acceptance/answer-format sections indicate thin instructions.
* **Verification mode** — dual (artifact + direct) > test_script > oracle.

All four sub-scores are arithmetic combinations of structural facts about
the task directory (file existence, lengths, counts, schema fields). No
semantic judgments — ZFC compliant. The composite is the equally-weighted
mean of the four sub-scores.

The promotion gate compares the composite against ``DEFAULT_THRESHOLD``
(0.5). Callers decide whether to quarantine sub-threshold tasks.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


DEFAULT_THRESHOLD: float = 0.5
_NEUTRAL: float = 0.6


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfidenceScore:
    """Composite confidence + per-signal breakdown for a single task."""

    task_id: str
    score: float
    threshold: float = DEFAULT_THRESHOLD
    breakdown: dict[str, float] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    @property
    def promoted(self) -> bool:
        """True when the task passes the promotion gate."""
        return self.score >= self.threshold

    def to_json(self) -> dict:
        payload = asdict(self)
        payload["promoted"] = self.promoted
        return payload


# ---------------------------------------------------------------------------
# Sub-scores
# ---------------------------------------------------------------------------


def _round(v: float) -> float:
    return round(v, 4)


def _score_size(expected_count: int) -> tuple[float, str]:
    """Score the ground-truth size for plausibility.

    The thresholds (3-50 sweet spot) are *tunable parameters*, not semantic
    judgments — they reflect mechanical heuristics about retrieval-task
    breadth that callers can override by passing a different scoring fn.
    """
    if expected_count <= 0:
        return 0.0, "empty ground truth"
    if expected_count <= 2:
        return 0.3, f"narrow ground truth ({expected_count} files)"
    if expected_count <= 50:
        return 1.0, f"plausible ground truth ({expected_count} files)"
    if expected_count <= 100:
        return 0.7, f"large ground truth ({expected_count} files)"
    return 0.3, f"very large ground truth ({expected_count} files) — likely unfocused"


def _score_instruction(instruction: str) -> tuple[float, str]:
    """Score the instruction body for quality (structural signal only)."""
    text = instruction.strip()
    if len(text) < 100:
        return 0.3, f"thin instruction ({len(text)} chars)"
    if "[...truncated]" in text:
        return 0.6, "instruction was auto-truncated"
    has_section = any(
        marker in text
        for marker in (
            "## Problem",
            "## Question",
            "## Task",
            "## Acceptance",
            "## Answer Format",
            "## Expected answer.json",
        )
    )
    if not has_section:
        return 0.5, "instruction lacks a recognized section header"
    return 1.0, "instruction looks well-formed"


_VERIFICATION_MODE_SCORES: dict[str, tuple[float, str]] = {
    "dual": (1.0, "dual-mode verification (artifact + direct)"),
    "test_script": (0.8, "direct test_script verification"),
    "oracle": (0.6, "oracle answer-comparison only"),
    "heuristic": (0.3, "heuristic verification"),
    "": (0.3, "verification mode unset"),
}


def _score_verification(
    verification: dict[str, object],
) -> tuple[float, str]:
    """Score verification rigor based on the metadata.json verification block."""
    mode = ""
    raw_mode = verification.get("verification_mode")
    if isinstance(raw_mode, str):
        mode = raw_mode
    if mode not in _VERIFICATION_MODE_SCORES:
        # Fall back on verification.type when verification_mode is unfamiliar
        raw_type = verification.get("type")
        if isinstance(raw_type, str) and raw_type in _VERIFICATION_MODE_SCORES:
            mode = raw_type
        else:
            return 0.3, f"unknown verification mode {mode!r}"
    return _VERIFICATION_MODE_SCORES[mode]


def _score_cross_source(
    task_id: str,
    cross_validation_report: dict | None,
) -> tuple[float, str]:
    """Score how well multiple curator backends agree for this task.

    Mapping (mechanical, no semantic judgments):

    * No report on disk → neutral (0.6)
    * Task absent from report → neutral (0.6)
    * Task in ``flagged_tasks`` → 0.3 (low confidence)
    * Otherwise → use the per-task ``min_f1`` directly as the sub-score
      (clamped to [0, 1]) when ≥ 1 pair was compared
    * Single-backend task (no comparison) → neutral (0.6)
    """
    if cross_validation_report is None:
        return _NEUTRAL, "no cross-validation report on disk"

    flagged = cross_validation_report.get("flagged_tasks") or []
    if isinstance(flagged, list) and task_id in flagged:
        return 0.3, "flagged by cross-validation (low backend agreement)"

    per_task = cross_validation_report.get("per_task") or []
    if not isinstance(per_task, list):
        return _NEUTRAL, "malformed cross-validation report"

    for entry in per_task:
        if not isinstance(entry, dict) or entry.get("task_id") != task_id:
            continue
        min_f1 = entry.get("min_f1")
        if min_f1 is None:
            return _NEUTRAL, "single-backend task (nothing to compare)"
        try:
            value = max(0.0, min(1.0, float(min_f1)))
        except (TypeError, ValueError):
            return _NEUTRAL, "non-numeric min_f1 in cross-validation report"
        return value, f"cross-validation min F1 = {value:.4f}"

    return _NEUTRAL, "task not present in cross-validation report"


# ---------------------------------------------------------------------------
# Filesystem readers
# ---------------------------------------------------------------------------


_MAX_INSTRUCTION_BYTES = 5 * 1024 * 1024


def _read_instruction(task_dir: Path) -> str:
    candidate = task_dir / "instruction.md"
    if not candidate.is_file():
        return ""
    try:
        if candidate.stat().st_size > _MAX_INSTRUCTION_BYTES:
            return ""
        return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _read_metadata(task_dir: Path) -> dict:
    path = task_dir / "metadata.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_ground_truth_size(task_dir: Path, metadata: dict) -> int:
    """Count expected files in ground_truth.json (top-level or tests/).

    Falls back to ``verification.oracle_answer`` length from metadata when
    no ground_truth.json is present (covers SDLC tasks at mining time
    where the GT path is set but the file may not yet exist).
    """
    for parent in (task_dir, task_dir / "tests"):
        gt = parent / "ground_truth.json"
        if not gt.is_file():
            continue
        try:
            data = json.loads(gt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        expected = data.get("expected") or data.get("answer")
        if isinstance(expected, list):
            return len(expected)

    verification = metadata.get("verification", {})
    if isinstance(verification, dict):
        oracle_answer = verification.get("oracle_answer", [])
        if isinstance(oracle_answer, list):
            return len(oracle_answer)
    return 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def load_cross_validation_report(tasks_dir: Path) -> dict | None:
    """Load the cross-validation report at ``<tasks_dir>/.codeprobe/...``.

    Returns ``None`` when the file is missing or malformed.
    """
    path = tasks_dir / ".codeprobe" / "cross_validation_report.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Cross-validation report unreadable: %s", path)
        return None
    return data if isinstance(data, dict) else None


def score_task_confidence(
    task_dir: Path,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    cross_validation_report: dict | None = None,
) -> ConfidenceScore:
    """Score the confidence of a single mined task.

    Args:
        task_dir: Path to the task directory.
        threshold: Promotion-gate threshold (default 0.5).
        cross_validation_report: Pre-loaded cross-validation report. If
            ``None``, the function attempts to load it from
            ``<task_dir>/../.codeprobe/cross_validation_report.json``.

    Returns:
        :class:`ConfidenceScore` with the composite score (in [0, 1]) and
        a per-signal breakdown so callers can present diagnostics.
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"threshold must be in [0, 1], got {threshold!r}")

    if cross_validation_report is None:
        cross_validation_report = load_cross_validation_report(task_dir.parent)

    metadata = _read_metadata(task_dir)
    instruction = _read_instruction(task_dir)
    expected_count = _read_ground_truth_size(task_dir, metadata)
    verification = (
        metadata.get("verification", {})
        if isinstance(metadata.get("verification"), dict)
        else {}
    )

    cross_score, cross_note = _score_cross_source(
        task_dir.name, cross_validation_report
    )
    size_score, size_note = _score_size(expected_count)
    instruction_score, instruction_note = _score_instruction(instruction)
    verification_score, verification_note = _score_verification(verification)

    breakdown = {
        "cross_source_agreement": _round(cross_score),
        "ground_truth_size": _round(size_score),
        "instruction_quality": _round(instruction_score),
        "verification_mode": _round(verification_score),
    }
    composite = _round(sum(breakdown.values()) / len(breakdown))

    return ConfidenceScore(
        task_id=task_dir.name,
        score=composite,
        threshold=threshold,
        breakdown=breakdown,
        notes={
            "cross_source_agreement": cross_note,
            "ground_truth_size": size_note,
            "instruction_quality": instruction_note,
            "verification_mode": verification_note,
        },
    )


def write_confidence_file(score: ConfidenceScore, task_dir: Path) -> Path:
    """Persist ``confidence.json`` next to the task's other artifacts."""
    out = task_dir / "confidence.json"
    out.write_text(
        json.dumps(score.to_json(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return out


def load_confidence_file(task_dir: Path) -> ConfidenceScore | None:
    """Load a previously-written confidence.json, or ``None`` if missing."""
    path = task_dir / "confidence.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return ConfidenceScore(
            task_id=str(data.get("task_id", task_dir.name)),
            score=float(data.get("score", 0.0)),
            threshold=float(data.get("threshold", DEFAULT_THRESHOLD)),
            breakdown=dict(data.get("breakdown") or {}),
            notes=dict(data.get("notes") or {}),
        )
    except (TypeError, ValueError):
        return None


def score_tasks_dir(
    tasks_dir: Path,
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> list[ConfidenceScore]:
    """Score every task under ``tasks_dir``, refreshing confidence.json files.

    Tasks are detected by the presence of either ``instruction.md`` or
    ``metadata.json``. The cross-validation report is loaded once and
    shared across tasks.
    """
    if not tasks_dir.is_dir():
        return []

    report = load_cross_validation_report(tasks_dir)
    scores: list[ConfidenceScore] = []
    for child in sorted(tasks_dir.iterdir()):
        if not child.is_dir():
            continue
        if not (
            (child / "instruction.md").is_file()
            or (child / "metadata.json").is_file()
        ):
            continue
        score = score_task_confidence(
            child,
            threshold=threshold,
            cross_validation_report=report,
        )
        write_confidence_file(score, child)
        scores.append(score)
    return scores


# ---------------------------------------------------------------------------
# Histogram helper for status display
# ---------------------------------------------------------------------------


def confidence_histogram(scores: list[ConfidenceScore]) -> dict[str, int]:
    """Return a stable-ordered count of tasks per confidence bucket.

    Buckets are inclusive of the lower bound and exclusive of the upper:

    * ``0.0-0.2``   — broken
    * ``0.2-0.4``   — low
    * ``0.4-0.5``   — borderline (just below the gate)
    * ``0.5-0.7``   — passing
    * ``0.7-0.9``   — strong
    * ``0.9-1.0``   — gold (1.0 lands here as well)
    """
    buckets = {
        "0.0-0.2": 0,
        "0.2-0.4": 0,
        "0.4-0.5": 0,
        "0.5-0.7": 0,
        "0.7-0.9": 0,
        "0.9-1.0": 0,
    }
    for s in scores:
        v = s.score
        if v < 0.2:
            buckets["0.0-0.2"] += 1
        elif v < 0.4:
            buckets["0.2-0.4"] += 1
        elif v < 0.5:
            buckets["0.4-0.5"] += 1
        elif v < 0.7:
            buckets["0.5-0.7"] += 1
        elif v < 0.9:
            buckets["0.7-0.9"] += 1
        else:
            buckets["0.9-1.0"] += 1
    return buckets
