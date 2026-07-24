"""R18 snapshot create — CSB layout on top of the r14 redaction pipeline.

The CSB layout emitted for every snapshot:

``{snapshot_dir}/``
  ``SNAPSHOT.json``                       — extended manifest (r14 + R18 dependency block)
  ``summary/{rewards,aggregate,timing,costs}.json``
  ``traces/{config}/{task_id}/``          — symlinks for ``hashes-only``, copies otherwise
  ``export/traces/{config}/{task_id}/``   — sanitised publish-time copies
  ``files/``                              — r14 redacted bodies (only when ``mode != hashes-only``)

Containment and safety invariants:

- Before any output is written, source symlinks are rejected by preflight and
  every regular file is captured through descriptor-relative, no-follow
  opens. In-place creation is likewise rejected before layout creation
  because it cannot preserve this immutable capture boundary.
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
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from codeprobe.snapshot.canary import CanaryResult
from codeprobe.snapshot.manifest import (
    ExtendedManifest,
    build_extended_manifest,
    collect_dependencies,
    manifest_to_json_dict,
)
from codeprobe.snapshot.redact import (
    PUBLISHABLE_DEFAULT,
    RedactionMode,
    _prepare_snapshot,
    _PreparedSnapshot,
    _write_materialized_bodies,
)
from codeprobe.snapshot.safe_io import (
    SecureOutputDirectory,
    SymlinkEscapeError,
    validate_source_tree,
)
from codeprobe.snapshot.scanners import Scanner

__all__ = [
    "CsbLayout",
    "FairnessLeakError",
    "SymlinkEscapeError",
    "create_snapshot",
    "preflight_symlink_containment",
]


class FairnessLeakError(RuntimeError):
    """Raised when ``create_snapshot`` is invoked with ``fairness_check=True``
    and the corpus contains oracle leaks. The snapshot is not produced; the
    caller fixes the leaking agent-facing file (or removes the offending
    task) and retries.
    """


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
    """Reject source symlinks before snapshot output is allocated.

    Secure capture intentionally accepts only regular files and directories.
    Even a currently-contained link can be swapped after a path-level
    containment check, so callers must materialize aliases before snapshot
    creation.
    """
    validate_source_tree(Path(root))


def _captured_trial_files(
    prepared: _PreparedSnapshot,
) -> dict[tuple[str, str], list[tuple[str, bytes]]]:
    skip_names = {"SNAPSHOT.json", "files"} | {
        sub.split("/", 1)[0] for sub in _LAYOUT_SUBDIRS
    }
    trials: dict[tuple[str, str], list[tuple[str, bytes]]] = {}
    for directory in prepared.source_directories:
        parts = PurePosixPath(directory).parts
        if len(parts) >= 2 and parts[0] not in skip_names:
            trials.setdefault((parts[0], parts[1]), [])
    for source_file in prepared.source_files:
        parts = PurePosixPath(source_file.relative_path).parts
        if len(parts) < 3 or parts[0] in skip_names:
            continue
        trial = (parts[0], parts[1])
        relative = PurePosixPath(*parts[2:]).as_posix()
        trials.setdefault(trial, []).append((relative, source_file.body))
    return trials


def _materialize_captured_layout(
    output: SecureOutputDirectory,
    prepared: _PreparedSnapshot,
    extended_manifest: ExtendedManifest,
    mode: RedactionMode,
) -> tuple[int, int]:
    for subdirectory in _LAYOUT_SUBDIRS:
        output.ensure_directory(subdirectory)

    _write_materialized_bodies(prepared, output)
    manifest_body = json.dumps(
        manifest_to_json_dict(extended_manifest),
        sort_keys=True,
        indent=2,
        separators=(",", ": "),
    ).encode()
    output.write_bytes("SNAPSHOT.json", manifest_body)

    redacted_files = {
        relative_path.removeprefix("files/"): body
        for relative_path, body in prepared.materialized_bodies
    }
    empty_summary = json.dumps({"entries": []}, indent=2).encode()
    for name in _SUMMARY_FILES:
        body = redacted_files.get(f"summary/{name}", empty_summary)
        output.write_bytes(f"summary/{name}", body)

    trials = _captured_trial_files(prepared)
    for (config, task_id), files in sorted(trials.items()):
        export_root = f"export/traces/{config}/{task_id}"
        output.ensure_directory(export_root)
        trace_root = f"traces/{config}/{task_id}"
        if mode == "hashes-only":
            output.symlink(
                trace_root,
                f"../../export/traces/{config}/{task_id}",
            )
        else:
            output.ensure_directory(trace_root)
            for relative, _source_body in files:
                source_relative = f"{config}/{task_id}/{relative}"
                body = redacted_files[source_relative]
                output.write_bytes(f"{export_root}/{relative}", body)
                output.write_bytes(f"{trace_root}/{relative}", body)

    output.ensure_path_unchanged()
    count = len(trials)
    return count, count


def _require_fairness_pass(
    prepared: _PreparedSnapshot,
    fairness_repo_root: Path | None,
) -> None:
    from codeprobe.snapshot.fairness import check_fairness

    with tempfile.TemporaryDirectory() as temporary:
        staged_root = Path(temporary) / "source"
        with SecureOutputDirectory(staged_root) as staged:
            for source_file in prepared.source_files:
                staged.write_bytes(source_file.relative_path, source_file.body)
            staged.ensure_path_unchanged()
        repo_root = fairness_repo_root if fairness_repo_root else staged_root
        fairness_result = check_fairness(
            task_roots=[staged_root],
            repo_root=repo_root,
        )
    if not fairness_result.ok:
        raise FairnessLeakError(
            f"Class E fairness scan found {len(fairness_result.leaks)} "
            f"leak(s) across {fairness_result.tasks_scanned} task(s); "
            "refusing to publish snapshot. "
            f"First leak: token={fairness_result.leaks[0].token!r} "
            f"location={fairness_result.leaks[0].location!r} "
            f"task_id={fairness_result.leaks[0].task_id!r}."
        )


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
    *,
    fairness_check: bool = False,
    fairness_repo_root: Path | None = None,
) -> dict[str, Any]:
    """Create a CSB-layout snapshot of ``experiment_dir`` at ``out_dir``.

    Parameters mirror :func:`codeprobe.snapshot.redact.redact` so the CLI can
    pass through user flags directly. The return value is a status dict
    suitable for JSON emission.

    When ``fairness_check`` is True, run :func:`check_fairness` against any
    task directories under ``experiment_dir`` before writing the snapshot.
    Leaks raise :class:`FairnessLeakError` and the snapshot is not produced.
    Use ``fairness_repo_root`` to point the static scan at a different repo
    (typically the codeprobe checkout); when None, ``experiment_dir`` is
    scanned for agent-facing files.

    For fail-closed descriptor semantics, ``experiment_dir`` must be a
    symlink-free tree and must differ from ``out_dir``. Unsupported inputs are
    rejected before any output directory is created.
    """
    experiment_dir = Path(experiment_dir)
    out_dir = Path(out_dir)
    if os.path.abspath(experiment_dir) == os.path.abspath(out_dir):
        raise SymlinkEscapeError(
            "in-place snapshot creation is unsupported by secure snapshot I/O"
        )

    # Capture and scan the full source through pinned, no-follow descriptors
    # before creating output. Layout population consumes only these captured
    # bytes, so a post-preflight source swap cannot redirect later copies.
    prepared = _prepare_snapshot(
        source_dir=experiment_dir,
        mode=mode,
        out_dir=out_dir,
        scanner=scanner,
        signing_key=signing_key,
        canary_proof=canary_proof,
        allow_source_in_export=allow_source_in_export,
    )
    if fairness_check:
        _require_fairness_pass(prepared, fairness_repo_root)

    # Extend with R18 schema_version + created_at + dependencies and write
    # the combined manifest to disk. The r14 attestation body is built from
    # the base manifest's canonical payload (excluding R18 fields), so the
    # attestation signature remains valid alongside the extended block.
    deps = collect_dependencies()
    extended = build_extended_manifest(prepared.manifest, dependencies=deps)
    with SecureOutputDirectory(out_dir) as output:
        trace_count, export_trace_count = _materialize_captured_layout(
            output,
            prepared,
            extended,
            mode,
        )

    # Status payload returned to the CLI (and suitable for direct tests).
    return {
        "status": "ok",
        "mode": prepared.manifest.mode,
        "out": str(out_dir.resolve()),
        "files": len(prepared.manifest.files),
        "schema_version": extended.schema_version,
        "created_at": extended.created_at,
        "traces": trace_count,
        "export_traces": export_trace_count,
        "attestation_kind": (
            prepared.manifest.attestation.kind
            if prepared.manifest.attestation
            else "missing"
        ),
        "dependencies": {
            "mcp_tools": len(extended.dependencies.mcp_tools),
            "llm_backends": len(extended.dependencies.llm_backends),
            "issue_trackers": len(extended.dependencies.issue_trackers),
            "build_manifest_parsers": len(extended.dependencies.build_manifest_parsers),
        },
    }
