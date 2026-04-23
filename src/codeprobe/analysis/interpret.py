"""Regression plotting for ``codeprobe interpret --regression``.

Walks a mined tasks directory, groups per ``task.id``, and renders a
per-task score-over-commit-history table. Uses
:attr:`codeprobe.models.task.TaskMetadata.ground_truth_commit_history`
(populated by the R20 refresh flow) so each row represents how a single
stable task has evolved over successive refreshes.

The renderer is deterministic and tolerates missing data: tasks without
a history show a single-commit baseline; tasks without aligned scores
show an em-dash placeholder. No semantic judgment is made here — only
IO and mechanical table formatting, so this module stays ZFC-compliant
(see ``analysis/stats.py`` justified-exception block in CLAUDE.md).
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "TaskRegression",
    "collect_task_regressions",
    "format_regression_report",
    "regression_report",
]


@dataclass(frozen=True)
class TaskRegression:
    """Per-task score trajectory across its commit history."""

    task_id: str
    commits: tuple[str, ...]        # oldest -> newest
    scores: tuple[float | None, ...]  # aligned with ``commits``; None = missing


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


def _iter_metadata_files(tasks_dir: Path) -> list[Path]:
    """Return every ``<tasks_dir>/*/metadata.json`` path."""
    if not tasks_dir.is_dir():
        return []
    out = []
    for child in sorted(tasks_dir.iterdir()):
        if not child.is_dir():
            continue
        md = child / "metadata.json"
        if md.is_file():
            out.append(md)
    return out


def _load_scores_for_commit(
    results_dir: Path | None,
    task_id: str,
    commit: str,
) -> float | None:
    """Best-effort lookup of a task's score at a given commit.

    We accept one of two layouts:

    1. ``<results_dir>/<commit>/scores.json`` mapping ``task_id -> score``.
    2. ``<results_dir>/scores.json`` mapping ``"<task_id>@<commit>" -> score``.

    Anything else returns ``None`` — the regression report is explicit
    about missing data rather than silently imputing values.
    """
    if results_dir is None or not results_dir.is_dir():
        return None

    per_commit = results_dir / commit / "scores.json"
    if per_commit.is_file():
        try:
            payload = json.loads(per_commit.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        val = payload.get(task_id)
        if isinstance(val, (int, float)):
            return float(val)

    flat = results_dir / "scores.json"
    if flat.is_file():
        try:
            payload = json.loads(flat.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        key = f"{task_id}@{commit}"
        val = payload.get(key)
        if isinstance(val, (int, float)):
            return float(val)
        val = payload.get(task_id)
        if isinstance(val, (int, float)):
            return float(val)

    return None


def collect_task_regressions(
    tasks_dir: Path,
    results_dir: Path | None = None,
) -> list[TaskRegression]:
    """Group ``<tasks_dir>/*/metadata.json`` by task.id into regressions.

    Each entry's ``commits`` is the task's
    :attr:`TaskMetadata.ground_truth_commit_history` if non-empty, else
    a single-element tuple built from ``ground_truth_commit`` for
    backwards-compatibility with tasks mined before R20.
    """
    regressions: list[TaskRegression] = []
    seen_ids: set[str] = set()
    for md_path in _iter_metadata_files(tasks_dir):
        try:
            meta = json.loads(md_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        task_id = meta.get("id")
        if not isinstance(task_id, str) or not task_id:
            continue
        if task_id in seen_ids:
            # Multiple task dirs with the same ID shouldn't happen (writer
            # dedupes on id), but be safe.
            continue
        seen_ids.add(task_id)

        metadata_section = meta.get("metadata") or {}
        history = metadata_section.get("ground_truth_commit_history") or ()
        if not history:
            single = metadata_section.get("ground_truth_commit") or ""
            commits: tuple[str, ...] = (single,) if single else ()
        else:
            commits = tuple(str(c) for c in history if c)

        scores = tuple(
            _load_scores_for_commit(results_dir, task_id, c) for c in commits
        )
        regressions.append(
            TaskRegression(task_id=task_id, commits=commits, scores=scores)
        )
    # Deterministic order: by task_id.
    regressions.sort(key=lambda r: r.task_id)
    return regressions


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _format_commit(sha: str) -> str:
    """Short 7-char form when the SHA looks hash-like; otherwise pass through."""
    if len(sha) >= 7 and all(c in "0123456789abcdefABCDEF" for c in sha):
        return sha[:7]
    return sha


def _format_score(score: float | None) -> str:
    if score is None:
        return "—"
    return f"{score:.2f}"


def format_regression_report(regressions: list[TaskRegression]) -> str:
    """Render regressions as a plain-text table.

    The output is deterministic and does not depend on rich being
    available; we prefer rich when importable for prettier alignment
    but the fallback stays identical in structure.
    """
    header_task = "Task ID"
    header_commits = "Commits (oldest -> newest)"
    header_scores = "Scores"

    if not regressions:
        return (
            f"{header_task:<20} {header_commits:<50} {header_scores}\n"
            "(no tasks with ground_truth_commit_history found)\n"
        )

    # Try rich first — the bead notes rich Text output is acceptable.
    try:
        from rich.console import Console
        from rich.table import Table

        buf = io.StringIO()
        console = Console(
            file=buf, force_terminal=False, color_system=None, width=120
        )
        table = Table(show_header=True, header_style="bold")
        table.add_column(header_task, no_wrap=True)
        table.add_column(header_commits)
        table.add_column(header_scores)
        for reg in regressions:
            commits_str = (
                " -> ".join(_format_commit(c) for c in reg.commits)
                if reg.commits
                else "(none)"
            )
            scores_str = (
                " -> ".join(_format_score(s) for s in reg.scores)
                if reg.scores
                else "—"
            )
            table.add_row(reg.task_id, commits_str, scores_str)
        console.print(table)
        return buf.getvalue()
    except ImportError:
        pass

    # Pure-stdlib fallback.
    lines = [
        f"{header_task:<20} {header_commits:<50} {header_scores}",
        "-" * 100,
    ]
    for reg in regressions:
        commits_str = (
            " -> ".join(_format_commit(c) for c in reg.commits)
            if reg.commits
            else "(none)"
        )
        scores_str = (
            " -> ".join(_format_score(s) for s in reg.scores)
            if reg.scores
            else "—"
        )
        lines.append(f"{reg.task_id:<20} {commits_str:<50} {scores_str}")
    return "\n".join(lines) + "\n"


def regression_report(
    tasks_dir: Path,
    results_dir: Path | None = None,
) -> str:
    """Top-level entrypoint used by ``codeprobe interpret --regression``."""
    regressions = collect_task_regressions(tasks_dir, results_dir=results_dir)
    return format_regression_report(regressions)
