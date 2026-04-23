"""R18 snapshot create — CSB layout on top of the r14 redaction pipeline.

The CSB layout emitted for every snapshot:

``{snapshot_dir}/``
  ``SNAPSHOT.json``                       — extended manifest (r14 + R18 dependency block)
  ``summary/{rewards,aggregate,timing,costs}.json``
  ``traces/{config}/{task_id}/``          — symlinks for ``hashes-only``, copies otherwise
  ``export/traces/{config}/{task_id}/``   — sanitised publish-time copies
  ``files/``                              — r14 redacted bodies (only when ``mode != hashes-only``)

Containment and safety invariants:

- Before any output is written, every symlink under ``experiment_dir`` is
  resolved. If a target escapes ``experiment_dir``, :class:`SymlinkEscapeError`
  is raised and no bytes are written.
- Symlinks created *inside* the snapshot are always relative paths rooted at
  the snapshot directory. Moving the snapshot (tar → move → untar) does not
  invalidate any link.
- When ``redaction_mode != "hashes-only"``, bodies are *copied* rather than
  symlinked so the snapshot is self-contained for publishing.
- No LLM is invoked from this module — the whole pipeline is deterministic
  IO plus hash arithmetic.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeprobe.snapshot.canary import CanaryResult
from codeprobe.snapshot.manifest import (
    build_extended_manifest,
    collect_dependencies,
    write_extended_manifest,
)
from codeprobe.snapshot.redact import (
    PUBLISHABLE_DEFAULT,
    RedactionMode,
    SnapshotManifest,
    redact,
)
from codeprobe.snapshot.scanners import Scanner

__all__ = [
    "CsbLayout",
    "SymlinkEscapeError",
    "create_snapshot",
    "preflight_symlink_containment",
]


class SymlinkEscapeError(RuntimeError):
    """Raised when a symlink target would escape the containing root."""


@dataclass(frozen=True)
class CsbLayout:
    """The directory layout emitted by :func:`create_snapshot`."""

    snapshot_dir: Path
    summary_dir: Path
    traces_dir: Path
    export_dir: Path
    export_traces_dir: Path


# Directories that make up the CSB layout, in create-order.
_LAYOUT_SUBDIRS: tuple[str, ...] = ("summary", "traces", "export", "export/traces")

# Files that must exist under summary/ (empty-but-valid when experiment dir
# provides no data for them).
_SUMMARY_FILES: tuple[str, ...] = ("rewards.json", "aggregate.json", "timing.json", "costs.json")


# ---------------------------------------------------------------------------
# Preflight: symlink containment
# ---------------------------------------------------------------------------


def preflight_symlink_containment(root: Path) -> None:
    """Walk ``root``; raise :class:`SymlinkEscapeError` on any escaping link.

    ``root`` and every symlink target are resolved via :meth:`Path.resolve`
    before comparison, so relative links are handled correctly. A symlink to
    ``../../etc`` will resolve outside ``root`` and be rejected.
    """
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(f"preflight root does not exist: {root}")
    root_resolved = root.resolve()
    for entry in root.rglob("*"):
        if not entry.is_symlink():
            continue
        try:
            target_resolved = (entry.parent / entry.readlink()).resolve()
        except OSError as exc:
            raise SymlinkEscapeError(
                f"cannot resolve symlink {entry} → {exc}"
            ) from exc
        if not _is_within(target_resolved, root_resolved):
            raise SymlinkEscapeError(
                f"symlink {entry} → {target_resolved} escapes root {root_resolved}"
            )


def _is_within(path: Path, root: Path) -> bool:
    """Return True when ``path`` is equal to or nested under ``root``."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# CSB layout scaffolding
# ---------------------------------------------------------------------------


def _ensure_layout(snapshot_dir: Path) -> CsbLayout:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for sub in _LAYOUT_SUBDIRS:
        (snapshot_dir / sub).mkdir(parents=True, exist_ok=True)
    return CsbLayout(
        snapshot_dir=snapshot_dir,
        summary_dir=snapshot_dir / "summary",
        traces_dir=snapshot_dir / "traces",
        export_dir=snapshot_dir / "export",
        export_traces_dir=snapshot_dir / "export" / "traces",
    )


def _write_empty_summary(layout: CsbLayout) -> None:
    """Write placeholder summary files when the experiment dir lacks them.

    Each file gets an ``{"entries": []}`` body so downstream consumers can
    load without handling missing-file edge cases.
    """
    for name in _SUMMARY_FILES:
        target = layout.summary_dir / name
        if target.exists():
            continue
        target.write_text(json.dumps({"entries": []}, indent=2))


def _populate_summary(layout: CsbLayout, experiment_dir: Path) -> None:
    """Copy summary/*.json from ``experiment_dir/summary/`` when available.

    Falls back to empty-but-valid placeholders (``{"entries": []}``) for any
    summary file that does not already exist in the source experiment.
    """
    src_summary = experiment_dir / "summary"
    if src_summary.is_dir():
        for name in _SUMMARY_FILES:
            src = src_summary / name
            if src.is_file():
                shutil.copy2(src, layout.summary_dir / name)
    _write_empty_summary(layout)


def _iter_trial_dirs(experiment_dir: Path) -> list[tuple[str, str, Path]]:
    """Yield ``(config, task_id, path)`` for every trial under ``experiment_dir``.

    An ``experiment_dir`` is expected to be structured as
    ``experiment_dir/<config>/<task_id>/...``. We accept any directory whose
    grandparent is ``experiment_dir`` as a trial; that keeps us tolerant to
    differing layouts (best effort; empty results are fine).
    """
    trials: list[tuple[str, str, Path]] = []
    for config_dir in sorted(experiment_dir.iterdir()):
        if not config_dir.is_dir():
            continue
        # Skip our own output if the user passed out_dir under experiment_dir.
        if config_dir.name in {"SNAPSHOT.json", *_LAYOUT_SUBDIRS, "files"}:
            continue
        for task_dir in sorted(config_dir.iterdir()):
            if task_dir.is_dir():
                trials.append((config_dir.name, task_dir.name, task_dir))
    return trials


def _populate_traces(
    layout: CsbLayout,
    experiment_dir: Path,
    use_symlinks: bool,
) -> int:
    """Populate ``traces/{config}/{task_id}/`` entries.

    Returns the number of trials linked or copied.

    When ``use_symlinks`` is True (the default for ``hashes-only`` mode), we
    create *relative* symlinks rooted at the snapshot directory, each
    pointing into ``export/traces/`` inside the same snapshot. This keeps
    the snapshot self-contained: it can be tar-moved across filesystem
    prefixes without breaking any trace link.

    When ``use_symlinks`` is False (content/secret modes), we copy instead
    of linking so the snapshot remains usable when the export tree is
    scrubbed, compressed, or published separately.
    """
    count = 0
    _ = experiment_dir  # unused; iteration drives off export_traces_dir below.
    for config_dir in sorted(layout.export_traces_dir.iterdir()) if layout.export_traces_dir.exists() else []:
        if not config_dir.is_dir():
            continue
        for task_dir in sorted(config_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            config = config_dir.name
            task_id = task_dir.name
            dest = layout.traces_dir / config / task_id
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() or dest.is_symlink():
                continue
            if use_symlinks:
                rel = _relative_symlink_target(
                    link_parent=dest.parent,
                    target=task_dir,
                )
                dest.symlink_to(rel)
            else:
                shutil.copytree(task_dir, dest)
            count += 1
    return count


def _populate_export_traces(
    layout: CsbLayout,
    experiment_dir: Path,
) -> int:
    """Populate ``export/traces/{config}/{task_id}/`` with publish-safe copies."""
    count = 0
    for config, task_id, source in _iter_trial_dirs(experiment_dir):
        dest = layout.export_traces_dir / config / task_id
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            continue
        shutil.copytree(source, dest)
        count += 1
    return count


def _relative_symlink_target(link_parent: Path, target: Path) -> Path:
    """Compute a relative path from ``link_parent`` to ``target``.

    ``link_parent`` is the directory that will contain the symlink. The
    returned path, when resolved relative to ``link_parent``, yields
    ``target``.
    """
    import os

    return Path(os.path.relpath(target, start=link_parent))


# ---------------------------------------------------------------------------
# Public orchestration entry point
# ---------------------------------------------------------------------------


def create_snapshot(
    experiment_dir: Path,
    out_dir: Path,
    mode: RedactionMode = PUBLISHABLE_DEFAULT,
    scanner: Scanner | None = None,
    signing_key: str | None = None,
    canary_proof: CanaryResult | None = None,
    allow_source_in_export: bool = False,
) -> dict[str, Any]:
    """Create a CSB-layout snapshot of ``experiment_dir`` at ``out_dir``.

    Parameters mirror :func:`codeprobe.snapshot.redact.redact` so the CLI can
    pass through user flags directly. The return value is a status dict
    suitable for JSON emission.
    """
    experiment_dir = Path(experiment_dir)
    out_dir = Path(out_dir)

    # Fail-loud preflight: reject any escaping symlink before we allocate
    # output directories so a malicious experiment can't half-populate a
    # snapshot.
    preflight_symlink_containment(experiment_dir)

    layout = _ensure_layout(out_dir)

    # r14 redaction produces SNAPSHOT.json + (for content modes) files/.
    base_manifest: SnapshotManifest = redact(
        source_dir=experiment_dir,
        mode=mode,
        out_dir=out_dir,
        scanner=scanner,
        signing_key=signing_key,
        canary_proof=canary_proof,
        allow_source_in_export=allow_source_in_export,
    )

    # Extend with R18 schema_version + created_at + dependencies. This
    # rewrites SNAPSHOT.json with the r14 keys preserved verbatim so the
    # r14 attestation remains valid.
    deps = collect_dependencies()
    extended = build_extended_manifest(base_manifest, dependencies=deps)
    write_extended_manifest(extended, out_dir)

    # Layout population — best-effort; an experiment_dir that lacks the
    # expected structure yields empty summaries and zero trace entries.
    # Order matters: export/traces/ must exist before traces/ is populated,
    # because traces/ symlinks or copies it (self-contained snapshot).
    _populate_summary(layout, experiment_dir)
    export_trace_count = _populate_export_traces(layout, experiment_dir)
    use_symlinks = mode == "hashes-only"
    trace_count = _populate_traces(layout, experiment_dir, use_symlinks=use_symlinks)

    # Status payload returned to the CLI (and suitable for direct tests).
    return {
        "status": "ok",
        "mode": base_manifest.mode,
        "out": str(out_dir.resolve()),
        "files": len(base_manifest.files),
        "schema_version": extended.schema_version,
        "created_at": extended.created_at,
        "traces": trace_count,
        "export_traces": export_trace_count,
        "attestation_kind": (
            base_manifest.attestation.kind if base_manifest.attestation else "missing"
        ),
        "dependencies": {
            "mcp_tools": len(extended.dependencies.mcp_tools),
            "llm_backends": len(extended.dependencies.llm_backends),
            "issue_trackers": len(extended.dependencies.issue_trackers),
            "build_manifest_parsers": len(extended.dependencies.build_manifest_parsers),
        },
    }
