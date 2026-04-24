"""Refresh-mode support for ``codeprobe mine --refresh``.

Re-mining a task directory against a new commit may or may not preserve
structural identity with the earlier version of the task. "Structural
identity" here is a mechanical, deterministic property:

- the ``oracle_type`` is the same (e.g. ``file_list`` vs ``count``), AND
- the ground-truth file set has changed by at most :data:`CHURN_THRESHOLD`
  (Jaccard-based churn).

When both hold, the refresh preserves the task_id and extends
``TaskMetadata.ground_truth_commit_history`` with the new commit SHA.
When either fails, :func:`refresh_task` raises
:class:`StructuralMismatchError` with a diff report — refusing to silently
renumber or overwrite the task. Callers that want to accept the change
must pass ``accept_structural_change=True``; we then renumber/update and
emit a fresh history rooted at the new commit.

ZFC compliance
--------------

All decisions here are deterministic arithmetic (Jaccard over file path
sets, equality of ``oracle_type`` strings). No heuristics, no semantic
judgment — this is mechanism, not policy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from codeprobe.models.task import Task

__all__ = [
    "CHURN_THRESHOLD",
    "RefreshDiff",
    "RefreshResult",
    "StructuralMismatchError",
    "StructuralSignature",
    "compute_diff",
    "jaccard",
    "read_structural_signature",
    "read_task_metadata_json",
    "refresh_task",
    "signature_from_task",
]


# Maximum allowed fraction of oracle files that may change between two
# commits while still counting as the "same" task. Matches INV1 from the
# R20 PRD: ``>20% oracle file churn`` triggers a fail-loud refresh.
CHURN_THRESHOLD: float = 0.20


@dataclass(frozen=True)
class StructuralSignature:
    """Mechanical identity signature for a task's ground truth.

    Two signatures represent the "same" task when :attr:`oracle_type`
    matches and the Jaccard distance between :attr:`oracle_files` is at
    most :data:`CHURN_THRESHOLD`.
    """

    oracle_type: str
    oracle_files: tuple[str, ...]  # sorted, deduped, whitespace-stripped

    @property
    def file_set(self) -> frozenset[str]:
        return frozenset(self.oracle_files)


@dataclass(frozen=True)
class RefreshDiff:
    """Structural diff between a task's old and new signatures."""

    old: StructuralSignature
    new: StructuralSignature
    churn: float
    oracle_type_changed: bool
    added_files: tuple[str, ...]
    removed_files: tuple[str, ...]

    @property
    def is_structural_mismatch(self) -> bool:
        """True iff the refresh would materially alter the task.

        Matches INV1: either the oracle type changed or Jaccard churn
        exceeds :data:`CHURN_THRESHOLD` (strict ``>``; equality passes).
        """
        return self.oracle_type_changed or self.churn > CHURN_THRESHOLD

    def as_report(self, task_dir: Path | str | None = None) -> str:
        """Human-readable diff used in CLI failure messages and tests.

        When ``task_dir`` is provided, the remediation section names the
        exact ``codeprobe mine --refresh <task_dir> --accept-structural-change``
        invocation so agents can copy/paste the fix instead of hunting for
        the flag. Without ``task_dir`` we fall back to a generic form.
        """
        lines = [
            "Structural mismatch detected between old and new ground truth:",
            f"  oracle_type: {self.old.oracle_type!r} -> {self.new.oracle_type!r}"
            + (" (CHANGED)" if self.oracle_type_changed else ""),
            f"  file churn:  {self.churn:.2%} "
            f"(threshold={CHURN_THRESHOLD:.0%})",
            f"  added files  ({len(self.added_files)}):",
        ]
        for f in self.added_files:
            lines.append(f"    + {f}")
        lines.append(f"  removed files ({len(self.removed_files)}):")
        for f in self.removed_files:
            lines.append(f"    - {f}")
        dir_token = str(task_dir) if task_dir is not None else "<task_dir>"
        lines.append(
            "To accept the change and renumber/update the task, run:\n"
            f"  codeprobe mine --refresh {dir_token} --accept-structural-change\n"
            "Or revert the repo to a commit within the allowed churn window."
        )
        return "\n".join(lines)


@dataclass(frozen=True)
class RefreshResult:
    """Outcome of a refresh operation."""

    task: Task
    diff: RefreshDiff
    preserved_id: bool  # True iff task.id was carried over from the old task
    renumbered: bool    # True iff accept_structural_change forced a rewrite


class StructuralMismatchError(Exception):
    """Raised when a refresh would materially change a task's ground truth.

    Carries the :class:`RefreshDiff` so the CLI can render a diff report
    and exit non-zero without ever silently overwriting the task. The
    optional ``task_dir`` is threaded through so :meth:`RefreshDiff.as_report`
    can emit the exact ``codeprobe mine --refresh <task_dir>
    --accept-structural-change`` invocation.
    """

    def __init__(
        self, diff: RefreshDiff, task_dir: Path | str | None = None
    ) -> None:
        super().__init__(diff.as_report(task_dir))
        self.diff = diff
        self.task_dir = task_dir


# ---------------------------------------------------------------------------
# Signature IO
# ---------------------------------------------------------------------------


def _normalize_files(raw: Any) -> tuple[str, ...]:
    """Coerce any iterable of path-likes into a sorted, deduped, stripped tuple."""
    if not raw:
        return ()
    if isinstance(raw, str):
        # A single string is almost certainly a mistake upstream; treat as
        # a one-element list for safety.
        raw = [raw]
    out: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        s = item.strip()
        if s:
            out.add(s)
    return tuple(sorted(out))


def _extract_oracle_fields(ground_truth: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Pull ``(oracle_type, oracle_files)`` from a ground_truth.json payload.

    Handles all three on-disk schemas emitted by :mod:`codeprobe.mining.writer`:

    - Oracle task: ``{oracle_type, expected, ...}``
    - Dual task:   ``{answer_type, answer, ...}``
    - Weighted-checklist SDLC: ``{schema_version: "sdlc-*", source_files, ...}``
    """
    oracle_type = ""
    raw_files: Any = ()

    if "oracle_type" in ground_truth:
        oracle_type = str(ground_truth.get("oracle_type") or "")
        raw_files = ground_truth.get("expected") or ()
    elif "answer_type" in ground_truth:
        oracle_type = str(ground_truth.get("answer_type") or "")
        if oracle_type == "file_list":
            raw_files = ground_truth.get("answer") or ()
        else:
            raw_files = ()
    else:
        schema = str(ground_truth.get("schema_version") or "")
        if schema.startswith("sdlc-") or schema == "weighted_checklist.v1":
            oracle_type = "weighted_checklist"
            raw_files = ground_truth.get("source_files") or ()
        else:
            oracle_type = "unknown"
            raw_files = ()

    return oracle_type, _normalize_files(raw_files)


def _read_ground_truth_json(task_dir: Path) -> dict[str, Any]:
    """Read a task's ground_truth.json, checking both legacy locations.

    Oracle tasks write to ``<task_dir>/ground_truth.json``; SDLC/dual tasks
    write to ``<task_dir>/tests/ground_truth.json``. We prefer the tests/
    variant when both exist (it's the newer layout) but fall back gracefully.
    """
    candidates = [
        task_dir / "tests" / "ground_truth.json",
        task_dir / "ground_truth.json",
    ]
    for path in candidates:
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"ground_truth.json at {path} is not valid JSON: {exc}"
                ) from exc
    raise FileNotFoundError(
        f"No ground_truth.json found in {task_dir} "
        f"(checked tests/ground_truth.json and ground_truth.json)"
    )


def read_task_metadata_json(task_dir: Path) -> dict[str, Any]:
    """Read and parse ``<task_dir>/metadata.json``.

    Raises :class:`FileNotFoundError` if the file is missing,
    :class:`ValueError` if it is present but unparseable — the refresh
    flow refuses to proceed on a corrupt task dir rather than silently
    renumbering.
    """
    path = task_dir / "metadata.json"
    if not path.is_file():
        raise FileNotFoundError(f"metadata.json not found in {task_dir}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"metadata.json at {path} is not valid JSON: {exc}") from exc


def read_structural_signature(task_dir: Path) -> StructuralSignature:
    """Extract the structural signature from an on-disk task directory."""
    gt = _read_ground_truth_json(task_dir)
    oracle_type, files = _extract_oracle_fields(gt)
    return StructuralSignature(oracle_type=oracle_type, oracle_files=files)


def signature_from_task(task: Task) -> StructuralSignature:
    """Build a structural signature from an in-memory :class:`Task`.

    Mirrors :func:`read_structural_signature` but sources its data from
    ``task.verification`` rather than ground_truth.json. Used by the
    refresh flow to compare a freshly-mined task against an on-disk
    predecessor without round-tripping through the filesystem.
    """
    v = task.verification
    if v.oracle_type:
        oracle_type = v.oracle_type
        raw_files: Any = v.oracle_answer
    elif v.verification_mode == "dual":
        # Legacy dual tasks may carry ``oracle_answer`` without an explicit
        # ``oracle_type``. The dual schema pins the answer shape to a
        # file list, so we recover it mechanically.
        oracle_type = "file_list"
        raw_files = v.oracle_answer
    else:
        # No explicit oracle_type on the in-memory model — treat as
        # weighted_checklist if the task carries SDLC-style verification,
        # else unknown. We don't have source_files on Task, so fall back
        # to empty file set and let the caller fail fast if needed.
        oracle_type = "weighted_checklist" if v.type == "test_script" else "unknown"
        raw_files = ()
    return StructuralSignature(
        oracle_type=oracle_type or "unknown",
        oracle_files=_normalize_files(raw_files),
    )


# ---------------------------------------------------------------------------
# Diff / churn
# ---------------------------------------------------------------------------


def jaccard(a: frozenset[str] | set[str], b: frozenset[str] | set[str]) -> float:
    """Return the Jaccard similarity of two sets.

    Convention used by this module: when both sets are empty we return
    ``1.0`` — two tasks with no oracle files are identical, not
    undefined. This keeps ``compute_diff`` total.
    """
    if not a and not b:
        return 1.0
    union = a | b
    if not union:
        return 1.0
    return len(a & b) / len(union)


def compute_diff(
    old: StructuralSignature, new: StructuralSignature
) -> RefreshDiff:
    """Compare two signatures and return the resulting :class:`RefreshDiff`."""
    old_set = old.file_set
    new_set = new.file_set
    churn = 1.0 - jaccard(old_set, new_set)
    added = tuple(sorted(new_set - old_set))
    removed = tuple(sorted(old_set - new_set))
    return RefreshDiff(
        old=old,
        new=new,
        churn=churn,
        oracle_type_changed=(old.oracle_type != new.oracle_type),
        added_files=added,
        removed_files=removed,
    )


# ---------------------------------------------------------------------------
# Refresh orchestration
# ---------------------------------------------------------------------------


def _preserve_history(
    existing_history: tuple[str, ...],
    existing_commit: str,
    new_commit: str,
) -> tuple[str, ...]:
    """Extend the commit history with ``new_commit`` without duplication.

    Seeds the history from the existing single-commit field if the
    pre-refresh task predates the history field. Order is strictly
    ``[oldest, ..., newest]``.
    """
    history = list(existing_history)
    if not history and existing_commit:
        history.append(existing_commit)
    if not history or history[-1] != new_commit:
        history.append(new_commit)
    return tuple(history)


def refresh_task(
    existing_task_dir: Path,
    new_task: Task,
    new_commit: str,
    *,
    accept_structural_change: bool = False,
) -> RefreshResult:
    """Merge a freshly-mined task into an existing task directory.

    The heart of R20. The function is pure up to a single filesystem
    read (``metadata.json`` + ``ground_truth.json`` of the existing dir):
    it does not write the refreshed task itself — callers are expected
    to hand the returned :attr:`RefreshResult.task` to
    :func:`codeprobe.mining.writer.write_task_dir`.

    Parameters
    ----------
    existing_task_dir:
        Directory on disk produced by a prior ``codeprobe mine`` run.
    new_task:
        Task freshly mined against ``new_commit``. May have a different
        ``id`` from the existing task — we override it on success.
    new_commit:
        SHA the new task was mined at. Appended to
        :attr:`TaskMetadata.ground_truth_commit_history`.
    accept_structural_change:
        When ``True``, bypass the fail-loud gate: even a mismatched
        refresh is accepted, but we renumber the task (reset history
        rather than extend it) so downstream consumers can detect the
        break via a history-length-of-one.

    Raises
    ------
    StructuralMismatchError
        When ``accept_structural_change`` is False and the diff exceeds
        INV1's allowed window (oracle_type change or >20% churn).
    FileNotFoundError
        When ``existing_task_dir`` has no metadata.json or no
        ground_truth.json.
    """
    old_meta_json = read_task_metadata_json(existing_task_dir)
    old_signature = read_structural_signature(existing_task_dir)
    new_signature = signature_from_task(new_task)
    diff = compute_diff(old_signature, new_signature)

    if diff.is_structural_mismatch and not accept_structural_change:
        # Fail loud — never silently renumber.
        raise StructuralMismatchError(diff, task_dir=existing_task_dir)

    old_task_id = old_meta_json.get("id")
    if not isinstance(old_task_id, str) or not old_task_id:
        raise ValueError(
            f"Existing task at {existing_task_dir} has no usable 'id' field "
            "in metadata.json; refusing to refresh."
        )

    old_metadata_section = old_meta_json.get("metadata") or {}
    existing_history = tuple(
        old_metadata_section.get("ground_truth_commit_history") or ()
    )
    existing_commit = str(old_metadata_section.get("ground_truth_commit") or "")

    if diff.is_structural_mismatch and accept_structural_change:
        # Renumber semantics: we keep task.id so downstream references
        # don't break, but we reset the commit history to root at the
        # new commit. A single-element history is the signal that a
        # structural break occurred.
        new_history: tuple[str, ...] = (new_commit,)
        renumbered = True
    else:
        new_history = _preserve_history(
            existing_history, existing_commit, new_commit
        )
        renumbered = False

    refreshed_metadata = replace(
        new_task.metadata,
        ground_truth_commit=new_commit,
        ground_truth_commit_history=new_history,
    )
    refreshed_task = replace(
        new_task,
        id=old_task_id,
        metadata=refreshed_metadata,
    )

    return RefreshResult(
        task=refreshed_task,
        diff=diff,
        preserved_id=True,
        renumbered=renumbered,
    )


# Re-export TaskMetadata so callers can build fresh new_task instances
# without importing a second module.
__all__.append("TaskMetadata")
