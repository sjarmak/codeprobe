"""Git worktree isolation for parallel task execution."""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import queue
import shutil
import subprocess
import tempfile
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from codeprobe.models.task import RepoRef

logger = logging.getLogger(__name__)


# Sentinel consumed by ``tests/test_isolation_scaffold.py`` to gate the
# sg-only scaffold-mode test suite. Flipped from absent to True when
# scaffold mode landed (codeprobe-yw6u, design in
# ``docs/investigations/codeprobe-2nw2/design.md``).
SGONLY_SCAFFOLD_AVAILABLE = True


# Shared cache directory for cloned secondary repos.  Sibling beads may
# override via environment or a future config; for now hardcode the
# documented location.
DEFAULT_REPO_CACHE_DIR = Path.home() / ".codeprobe" / "repo-cache"

# Process-level lock to serialise cache clones so concurrent task
# executions don't race on the same on-disk directory.
_cache_lock = threading.Lock()


def _coerce_repo_ref(value: object) -> RepoRef:
    """Accept either a RepoRef or a mapping with the expected keys."""
    if isinstance(value, RepoRef):
        return value
    if isinstance(value, dict):
        return RepoRef(
            name=str(value.get("name", "")),
            ground_truth_commit=str(value.get("ground_truth_commit", "")),
            url=str(value.get("url", "")),
            local_path=str(value.get("local_path", "")),
        )
    raise TypeError(f"Cannot coerce {type(value).__name__} to RepoRef")


def _discover_experiment_dirs(workdir: Path) -> list[str]:
    """Find top-level directories that contain an experiment.json.

    These are codeprobe experiment directories that must survive git clean.
    """
    excludes: list[str] = []
    try:
        for entry in workdir.iterdir():
            if entry.is_dir() and (entry / "experiment.json").is_file():
                excludes.append(entry.name)
    except OSError:
        pass
    return excludes


def _active_top_level_name(repo_path: Path, active_experiment_dir: Path) -> str | None:
    """Return the name of the top-level child of *repo_path* that contains
    *active_experiment_dir*, or ``None`` if it does not resolve under *repo_path*.
    """
    try:
        repo_resolved = repo_path.resolve()
        active_resolved = active_experiment_dir.resolve()
        rel = active_resolved.relative_to(repo_resolved)
    except (OSError, ValueError):
        return None
    parts = rel.parts
    if not parts:
        return None
    return parts[0]


@contextlib.contextmanager
def quarantine_sibling_experiments(
    repo_path: Path,
    active_experiment_dir: Path,
) -> Iterator[None]:
    """Hide sibling experiment directories from the agent during a run.

    Sibling experiment dirs (top-level entries of *repo_path* that contain an
    ``experiment.json`` other than the one belonging to *active_experiment_dir*)
    are atomically moved to a temporary quarantine directory on enter and
    restored on exit, including on exception.

    Without this, an agent running inside a slot worktree under
    ``<repo>/.codeprobe-worktrees-*/slot-N`` can still ``cd ../../`` to the
    real repo root and read another experiment's ``ground_truth.json`` —
    leaking the answer key for the active run.
    """
    repo_resolved = repo_path.resolve()
    if not repo_resolved.is_dir():
        yield
        return

    active_top = _active_top_level_name(repo_resolved, active_experiment_dir)
    if active_top is None:
        logger.warning(
            "quarantine_sibling_experiments: active experiment %s is not under "
            "repo %s; skipping quarantine to avoid hiding unrelated dirs",
            active_experiment_dir,
            repo_path,
        )
        yield
        return

    sibling_names = [
        name
        for name in _discover_experiment_dirs(repo_resolved)
        if name != active_top
    ]
    if not sibling_names:
        yield
        return

    quarantine_dir = repo_resolved / f".codeprobe-quarantine-{uuid.uuid4().hex[:8]}"
    moved: list[tuple[Path, Path]] = []  # (original, quarantined)

    def _restore() -> None:
        for original, quarantined in moved:
            try:
                if quarantined.exists() and not original.exists():
                    shutil.move(str(quarantined), str(original))
            except OSError as exc:  # pragma: no cover — defensive
                logger.warning(
                    "Failed to restore quarantined experiment dir %s -> %s: %s",
                    quarantined,
                    original,
                    exc,
                )
        if quarantine_dir.exists():
            try:
                shutil.rmtree(quarantine_dir)
            except OSError as exc:  # pragma: no cover — defensive
                logger.warning(
                    "Failed to remove quarantine dir %s: %s", quarantine_dir, exc
                )

    try:
        quarantine_dir.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        logger.warning(
            "quarantine_sibling_experiments: cannot create quarantine dir %s: %s; "
            "siblings will remain visible",
            quarantine_dir,
            exc,
        )
        yield
        return

    try:
        for name in sibling_names:
            src = repo_resolved / name
            dst = quarantine_dir / name
            try:
                shutil.move(str(src), str(dst))
                moved.append((src, dst))
            except OSError as exc:
                logger.warning(
                    "quarantine_sibling_experiments: failed to move %s -> %s: %s; "
                    "rolling back partial quarantine",
                    src,
                    dst,
                    exc,
                )
                _restore()
                yield
                return
    except BaseException:
        _restore()
        raise

    try:
        yield
    finally:
        _restore()


# Names always kept inside the workspace under quarantine_local_source.
# .git is needed for any in-workspace git operations the agent might
# perform (and for codeprobe's own pin/restore plumbing). The
# .codeprobe-* prefixes cover worktree pool dirs and codeprobe metadata
# that the executor relies on between trials.
_LOCAL_SOURCE_DEFAULT_KEEP: frozenset[str] = frozenset({".git", ".codeprobe"})


# scaffold-mode: extensions that get 0-byte placeholders left at their
# original paths so the agent can edit-in-place during a sg-only run.
# Mirrors CodeScaleBench ``generate_sgonly_dockerfiles.py`` lines
# 283-326 verbatim — this list is a security boundary (truncate
# surface), not a convenience filter. Update in lockstep with CSB.
# See ``docs/investigations/codeprobe-2nw2/design.md`` §TRUNCATE_EXTENSIONS.
_SCAFFOLD_EXTENSIONS: frozenset[str] = frozenset({
    # Python
    ".py", ".pyx", ".pyi",
    # JavaScript / TypeScript
    ".js", ".ts", ".jsx", ".tsx", ".mjs", ".cjs", ".mts", ".cts",
    # Go
    ".go",
    # Java / JVM
    ".java", ".kt", ".scala", ".groovy", ".clj",
    # C / C++
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    # Rust
    ".rs",
    # Ruby
    ".rb",
    # C# / .NET
    ".cs", ".fs",
    # Swift / Objective-C
    ".swift", ".m", ".mm",
    # Web frameworks
    ".vue", ".svelte",
    # Shell
    ".sh", ".bash", ".zsh",
    # Lua
    ".lua",
    # Protobuf / gRPC / IDL
    ".proto", ".thrift", ".avsc", ".fbs",
    # Config / data
    ".yaml", ".yml", ".toml", ".json", ".xml", ".ini", ".cfg",
    # Documentation
    ".md", ".rst", ".txt", ".adoc",
    # Build files
    ".cmake", ".bzl", ".bazel",
    # SQL
    ".sql",
    # Erlang / Elixir
    ".erl", ".ex", ".exs",
    # PHP
    ".php",
    # Perl
    ".pl", ".pm",
    # R
    ".r",
})

# scaffold-mode: directory names that, when present anywhere in a path,
# disqualify the file from getting a placeholder OR from being captured
# in the agent overlay. ``.git`` and ``.codeprobe*`` are also in the
# keep set so they're not actually in the stash; listing them here is
# defense-in-depth.
_SCAFFOLD_SKIP_DIR_NAMES: frozenset[str] = frozenset({
    ".git",
    ".codeprobe",
    "node_modules",
    "tests",
})

# scaffold-mode: extra prefixes excluded from the overlay capture.
# Mirrors CSB ``backup_agent_files`` plus codeprobe-specific paths
# called out in the design doc. Workflow files in particular must
# never be overlaid (would let the agent inject CI).
_SCAFFOLD_OVERLAY_EXCLUDED_RELPREFIXES: tuple[str, ...] = (
    ".git/",
    ".codeprobe/",
    ".claude/",
    "tests/",
    ".github/workflows/",
)


def _matches_scaffold_extension(path: Path) -> bool:
    """Return True if *path*'s suffix is in :data:`_SCAFFOLD_EXTENSIONS`."""
    # ``Path.suffix`` is empty for dotfiles like ``.gitignore``; that
    # matches the design — those are not in TRUNCATE_EXTENSIONS.
    return path.suffix.lower() in _SCAFFOLD_EXTENSIONS


def _path_under_skip_dir(rel_parts: tuple[str, ...]) -> bool:
    """Return True if any ancestor segment of *rel_parts* is a skip dir."""
    # ``rel_parts`` is the path components RELATIVE to the stash or
    # workspace root. We exclude the last segment (the filename) and
    # check parent directories against the skip set.
    for part in rel_parts[:-1]:
        if part in _SCAFFOLD_SKIP_DIR_NAMES:
            return True
    return False


def _collect_scaffold_paths(stash_dir: Path) -> list[str]:
    """Walk *stash_dir* and return workspace-relative POSIX paths for
    every file under :data:`_SCAFFOLD_EXTENSIONS` not under a skip dir.

    The stash mirrors the original workspace layout, so paths returned
    here are exactly the relative paths where 0-byte placeholders must
    be created in the workspace.
    """
    paths: list[str] = []
    for file_path in stash_dir.rglob("*"):
        if not file_path.is_file() or file_path.is_symlink():
            continue
        try:
            rel = file_path.relative_to(stash_dir)
        except ValueError:  # pragma: no cover — defensive
            continue
        rel_parts = rel.parts
        if _path_under_skip_dir(rel_parts):
            continue
        if not _matches_scaffold_extension(file_path):
            continue
        paths.append(rel.as_posix())
    return paths


def _create_scaffold_placeholders(
    workspace: Path, scaffold_paths: list[str]
) -> None:
    """Create 0-byte placeholder files in *workspace* for each path."""
    for rel in scaffold_paths:
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()


def _write_scaffold_manifest(
    stash_dir: Path, scaffold_paths: list[str]
) -> None:
    """Write ``manifest.json`` inside *stash_dir* describing the scaffold."""
    payload = {
        "mode": "scaffold",
        "stash_dir": str(stash_dir),
        "scaffold_paths": sorted(scaffold_paths),
        "created_at": _dt.datetime.now(_dt.UTC)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    (stash_dir / "manifest.json").write_text(json.dumps(payload, indent=2))


def _overlay_excluded(rel_posix: str) -> bool:
    """Return True if a workspace-relative path is excluded from overlay.

    Mirrors the design-doc filter rules: ``.git/``, ``.codeprobe/``,
    ``.claude/``, ``tests/``, and ``.github/workflows/`` are all
    rejected. Anywhere-named ``experiment.json`` and any path under a
    directory containing an ``experiment.json`` are also excluded; the
    caller passes those in via *experiment_excluded_prefixes*.
    """
    for prefix in _SCAFFOLD_OVERLAY_EXCLUDED_RELPREFIXES:
        if rel_posix == prefix.rstrip("/") or rel_posix.startswith(prefix):
            return True
    return False


def _collect_overlay_files(
    workspace: Path,
    scaffold_paths_set: frozenset[str],
    keep_set: set[str],
    experiment_dir_names: frozenset[str],
) -> list[str]:
    """Capture the agent overlay: files to copy out of the workspace.

    A file qualifies if it's a non-symlink regular file with size > 0
    (or, for scaffold paths, size > 0 means the agent grew it from
    the placeholder), is not under any excluded prefix, and is not a
    sibling experiment dir.
    """
    overlay: list[str] = []
    for file_path in workspace.rglob("*"):
        if not file_path.is_file() or file_path.is_symlink():
            continue
        try:
            rel = file_path.relative_to(workspace)
        except ValueError:  # pragma: no cover — defensive
            continue
        rel_posix = rel.as_posix()
        # Untouched 0-byte placeholders: skip (real source wins).
        try:
            size = file_path.stat().st_size
        except OSError:
            continue
        if rel_posix in scaffold_paths_set and size == 0:
            continue
        # Files of any size that aren't placeholders: still need to
        # filter against the overlay-exclusion rules. (Agent-written
        # 0-byte answer files are intentionally discarded — codeprobe's
        # oracle assumes meaningful content.)
        if size == 0 and rel_posix not in scaffold_paths_set:
            continue
        # Top-level keep / worktree-pool / stash dirs.
        top = rel.parts[0] if rel.parts else ""
        if top in keep_set:
            continue
        if top.startswith(".codeprobe-worktrees"):
            continue
        if top.startswith(".codeprobe-source-stash-"):
            continue
        if top in experiment_dir_names:
            continue
        # Path-prefix excludes (.git/, tests/, …).
        if _overlay_excluded(rel_posix):
            continue
        # Anywhere-named experiment.json.
        if file_path.name == "experiment.json":
            continue
        overlay.append(rel_posix)
    return overlay


@contextlib.contextmanager
def quarantine_local_source(
    workspace: Path,
    *,
    keep: tuple[str, ...] = (),
    mode: Literal["hide", "scaffold"] = "hide",
) -> Iterator[None]:
    """Hide or scaffold local source files during a sg-only run.

    ``mode="hide"`` (default; codeprobe-jf28 behaviour) atomically
    moves every non-kept top-level entry to a sibling stash on enter
    and restores it on exit. Files the agent creates during the yield
    window (e.g. ``answer.txt``) survive the restore; agent edits to
    same-named entries win over the stash.

    ``mode="scaffold"`` (codeprobe-2nw2) does everything ``hide`` does
    AND leaves 0-byte placeholder files at every path under
    :data:`_SCAFFOLD_EXTENSIONS` so the agent can edit-in-place. On
    exit, the context manager runs the 6-step overlay contract from
    ``docs/investigations/codeprobe-2nw2/design.md``:

    1. Yield ends.
    2. Capture every file the agent wrote (placeholders grown 0→N
       bytes plus brand-new non-excluded files) into
       ``<stash>/__agent_overlay__/<relpath>``.
    3. Remove the scaffold placeholders and the originals of the
       overlay files from the workspace.
    4. Restore the stashed source.
    5. Copy the overlay back on top of the restored source.
    6. Remove the stash dir (manifest dies with it).

    On an exception inside the yield window the agent overlay is
    discarded — source is restored unconditionally so the next trial
    starts from a clean state.

    Mirrors the CSB ``Dockerfile.sg_only`` and EB
    ``generate_sg_only_dockerfile`` pattern: the workspace appears
    empty so the agent must use Sourcegraph MCP for code access.

    The stash sits in a temp directory placed next to *workspace*
    when that's writable, or in the system temp directory otherwise.
    Same-filesystem placement enables atomic rename for performance
    on large source trees.
    """
    workspace_resolved = workspace.resolve()
    if not workspace_resolved.is_dir():
        yield
        return

    keep_set = set(_LOCAL_SOURCE_DEFAULT_KEEP) | set(keep)

    def _is_quarantinable(name: str) -> bool:
        if name in keep_set:
            return False
        # Cover the runtime-suffixed worktree pool dirs that
        # WorktreeIsolation creates under ``<repo>/.codeprobe-worktrees-<ns>``.
        if name.startswith(".codeprobe-worktrees"):
            return False
        if name.startswith(".codeprobe-source-stash-"):
            return False
        return True

    try:
        entries = [e for e in workspace_resolved.iterdir() if _is_quarantinable(e.name)]
    except OSError as exc:
        logger.warning(
            "quarantine_local_source: cannot enumerate %s: %s; "
            "source will remain visible",
            workspace_resolved,
            exc,
        )
        yield
        return

    if not entries:
        yield
        return

    # Place the stash next to the workspace when possible so the move
    # is a same-filesystem rename. Fall back to the system temp dir
    # if the parent is not writable.
    stash_parent_candidates: list[Path] = []
    if workspace_resolved.parent.is_dir():
        stash_parent_candidates.append(workspace_resolved.parent)
    stash_parent_candidates.append(Path(tempfile.gettempdir()))

    stash_dir: Path | None = None
    for candidate in stash_parent_candidates:
        try:
            stash_dir = Path(
                tempfile.mkdtemp(prefix=".codeprobe-source-stash-", dir=str(candidate))
            )
            break
        except OSError as exc:
            logger.debug(
                "quarantine_local_source: stash creation failed under %s: %s",
                candidate,
                exc,
            )

    if stash_dir is None:
        logger.warning(
            "quarantine_local_source: could not create stash dir in any candidate; "
            "source will remain visible"
        )
        yield
        return

    moved: list[tuple[Path, Path]] = []  # (original, stashed)
    scaffold_paths: list[str] = []
    # Top-level experiment dirs at scaffold-creation time. Used by the
    # overlay capture to skip sibling experiments that the agent should
    # not be able to overwrite.
    experiment_dir_names = frozenset(
        _discover_experiment_dirs(workspace_resolved)
    )

    def _restore_hide() -> None:
        """Default restore (hide mode and shared error path).

        Keeps agent-written same-named entries. Drops the stash dir
        unconditionally at the end.
        """
        for original, stashed in moved:
            try:
                if not stashed.exists():
                    continue
                if original.exists():
                    if stashed.is_dir() and not stashed.is_symlink():
                        shutil.rmtree(stashed)
                    else:
                        stashed.unlink()
                else:
                    shutil.move(str(stashed), str(original))
            except OSError as exc:  # pragma: no cover — defensive
                logger.warning(
                    "Failed to restore quarantined source %s -> %s: %s",
                    stashed,
                    original,
                    exc,
                )
        if stash_dir is not None and stash_dir.exists():
            try:
                shutil.rmtree(stash_dir)
            except OSError as exc:  # pragma: no cover — defensive
                logger.warning(
                    "Failed to remove source-stash dir %s: %s", stash_dir, exc
                )

    def _restore_scaffold_exception() -> None:
        """Scaffold-mode exception path: force source back, drop agent state.

        Walks the workspace and deletes every non-kept entry (these
        are the scaffold placeholders plus anything the agent wrote
        before crashing), then moves stashed source back into place,
        then removes the stash dir.
        """
        try:
            for entry in list(workspace_resolved.iterdir()):
                if not _is_quarantinable(entry.name):
                    continue
                try:
                    if entry.is_dir() and not entry.is_symlink():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                except OSError as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "Failed to clear scaffold artefact %s: %s", entry, exc
                    )
        except OSError as exc:  # pragma: no cover — defensive
            logger.warning(
                "Failed to enumerate workspace during scaffold rollback: %s", exc
            )
        for original, stashed in moved:
            try:
                if stashed.exists():
                    shutil.move(str(stashed), str(original))
            except OSError as exc:  # pragma: no cover — defensive
                logger.warning(
                    "Failed to restore scaffold source %s -> %s: %s",
                    stashed,
                    original,
                    exc,
                )
        if stash_dir is not None and stash_dir.exists():
            try:
                shutil.rmtree(stash_dir)
            except OSError as exc:  # pragma: no cover — defensive
                logger.warning(
                    "Failed to remove scaffold stash dir %s: %s", stash_dir, exc
                )

    def _scaffold_normal_exit() -> None:
        """6-step overlay contract — see design doc §__exit__ ordering."""
        scaffold_set = frozenset(scaffold_paths)
        overlay_root = stash_dir / "__agent_overlay__"
        # Step 2: capture overlay.
        overlay_relpaths = _collect_overlay_files(
            workspace_resolved,
            scaffold_set,
            keep_set,
            experiment_dir_names,
        )
        overlay_root.mkdir(exist_ok=True)
        for rel in overlay_relpaths:
            src_file = workspace_resolved / rel
            dst_file = overlay_root / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(src_file, dst_file)
            except OSError as exc:  # pragma: no cover — defensive
                logger.warning(
                    "scaffold overlay capture failed for %s: %s", src_file, exc
                )
        # Step 3: delete scaffold placeholders + workspace originals of
        # overlay files. Placeholders first (cheap), then any non-empty
        # files captured.
        for rel in scaffold_paths:
            target = workspace_resolved / rel
            if target.exists() and not target.is_dir():
                try:
                    target.unlink()
                except OSError as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "Failed to remove scaffold placeholder %s: %s", target, exc
                    )
        for rel in overlay_relpaths:
            target = workspace_resolved / rel
            if target.exists():
                try:
                    if target.is_dir() and not target.is_symlink():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                except OSError as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "Failed to remove overlay source %s: %s", target, exc
                    )
        # After steps 2-3 the workspace contains only the keep set
        # plus possibly-empty parent directories the scaffolds lived
        # in. Step 4 moves stashed entries back; if a top-level
        # placeholder parent dir was created by scaffold mode (e.g.
        # ``src/`` had only ``.py`` files all scaffolded), it now
        # blocks the stash move and must be removed first.
        for original, stashed in moved:
            if original.exists() and original.is_dir() and not original.is_symlink():
                try:
                    shutil.rmtree(original)
                except OSError as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "Failed to clear scaffold parent dir %s: %s", original, exc
                    )
            elif original.exists():
                try:
                    original.unlink()
                except OSError as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "Failed to clear scaffold leftover %s: %s", original, exc
                    )
        # Step 4: restore stash.
        for original, stashed in moved:
            if stashed.exists():
                try:
                    shutil.move(str(stashed), str(original))
                except OSError as exc:  # pragma: no cover — defensive
                    logger.warning(
                        "Failed to restore scaffold source %s -> %s: %s",
                        stashed,
                        original,
                        exc,
                    )
        # Step 5: overlay agent files on top of restored source.
        for rel in overlay_relpaths:
            src_file = overlay_root / rel
            dst_file = workspace_resolved / rel
            if not src_file.exists():
                continue
            dst_file.parent.mkdir(parents=True, exist_ok=True)
            try:
                if dst_file.exists():
                    if dst_file.is_dir() and not dst_file.is_symlink():
                        shutil.rmtree(dst_file)
                    else:
                        dst_file.unlink()
                shutil.copy2(src_file, dst_file)
            except OSError as exc:  # pragma: no cover — defensive
                logger.warning(
                    "Failed to apply overlay %s -> %s: %s",
                    src_file,
                    dst_file,
                    exc,
                )
        # Step 6: clean stash (manifest, overlay copies, and any
        # leftover stashed dirs all die together).
        if stash_dir is not None and stash_dir.exists():
            try:
                shutil.rmtree(stash_dir)
            except OSError as exc:  # pragma: no cover — defensive
                logger.warning(
                    "Failed to remove scaffold stash dir %s: %s", stash_dir, exc
                )

    # ---- Stash top-level entries (shared by both modes). -----------
    try:
        for entry in entries:
            src = entry
            dst = stash_dir / entry.name
            try:
                shutil.move(str(src), str(dst))
                moved.append((src, dst))
            except OSError as exc:
                logger.warning(
                    "quarantine_local_source: failed to move %s -> %s: %s; "
                    "rolling back partial quarantine",
                    src,
                    dst,
                    exc,
                )
                _restore_hide()
                yield
                return
    except BaseException:
        _restore_hide()
        raise

    # ---- Scaffold-only: create placeholders + manifest. ------------
    if mode == "scaffold":
        try:
            scaffold_paths = _collect_scaffold_paths(stash_dir)
            _create_scaffold_placeholders(workspace_resolved, scaffold_paths)
            _write_scaffold_manifest(stash_dir, scaffold_paths)
        except BaseException:
            _restore_scaffold_exception()
            raise

    success = False
    try:
        yield
        success = True
    finally:
        if mode == "scaffold":
            if success:
                _scaffold_normal_exit()
            else:
                _restore_scaffold_exception()
        else:
            _restore_hide()


def git_restore_clean(workdir: Path, *, extra_excludes: tuple[str, ...] = ()) -> None:
    """Restore tracked files and remove untracked files in *workdir*.

    Uses ``git restore .`` (tolerant of empty diffs) followed by
    ``git clean -fd``.  Always excludes ``.codeprobe``,
    ``.codeprobe-worktrees``, and any directories containing
    ``experiment.json`` (codeprobe experiment dirs).
    """
    result = subprocess.run(
        ["git", "restore", "."],
        cwd=workdir,
        capture_output=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        # "could not resolve HEAD" is expected in a truly empty/detached
        # worktree — not worth warning about.
        if "could not resolve" not in stderr:
            logger.debug("git restore in %s: %s", workdir, stderr)
    clean_cmd = [
        "git",
        "clean",
        "-fd",
        "-e",
        ".codeprobe",
        "-e",
        ".codeprobe-worktrees*",
    ]
    # Auto-discover experiment directories inside the repo
    for exp_dir in _discover_experiment_dirs(workdir):
        clean_cmd += ["-e", exp_dir]
    for exc in extra_excludes:
        clean_cmd += ["-e", exc]
    subprocess.run(clean_cmd, cwd=workdir, check=True, capture_output=True)


def git_pin_commit(workdir: Path, commit: str) -> None:
    """Checkout a specific commit in *workdir* (detached HEAD).

    Used to pin a worktree or repo to the parent of a merge commit so
    the agent starts from the pre-merge state.

    Raises ``subprocess.CalledProcessError`` if the commit is unreachable.
    """
    subprocess.run(
        ["git", "checkout", "--detach", commit],
        cwd=workdir,
        check=True,
        capture_output=True,
    )


def _ensure_cached_clone(url: str, name: str, cache_dir: Path) -> Path:
    """Ensure a bare-ish clone of *url* exists at ``cache_dir/name``.

    Performs ``git clone`` if missing, otherwise reuses the existing
    checkout (callers copy from the cache into the workspace so the
    cache itself is never mutated per-task).
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    target = cache_dir / name
    with _cache_lock:
        if target.exists():
            return target
        subprocess.run(
            ["git", "clone", url, str(target)],
            check=True,
            capture_output=True,
        )
    return target


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy *src* to *dst*, preserving symlinks.  Overwrites *dst*."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True)


def setup_multi_repo_workspace(
    workspace: Path,
    additional_repos: list[RepoRef] | list[dict],
    cache_dir: Path = DEFAULT_REPO_CACHE_DIR,
) -> list[Path]:
    """Lay out secondary repos as ``workspace/repos/<name>`` and pin each.

    For every RepoRef:

    1. If ``local_path`` is set, copy that directory into
       ``workspace/repos/<name>``.
    2. Otherwise, clone (or reuse cache) from ``url`` at
       ``cache_dir/<name>``, then copy into the workspace.
    3. Pin the resulting workspace copy to ``ground_truth_commit^`` via
       :func:`git_pin_commit`.

    Returns the list of workspace-relative repo paths (one per input).

    On failure, any already-created repo dirs are removed so the caller
    never observes a half-pinned state.
    """
    repos_root = workspace / "repos"
    repos_root.mkdir(parents=True, exist_ok=True)

    created: list[Path] = []
    try:
        for raw in additional_repos:
            ref = _coerce_repo_ref(raw)
            target = repos_root / ref.name
            if ref.local_path:
                src = Path(ref.local_path)
                if not src.is_dir():
                    raise FileNotFoundError(f"RepoRef local_path does not exist: {src}")
                _copy_tree(src, target)
            else:
                cached = _ensure_cached_clone(ref.url, ref.name, cache_dir)
                _copy_tree(cached, target)
            created.append(target)
            git_pin_commit(target, f"{ref.ground_truth_commit}^")
    except Exception:
        # Roll back partial state so the caller never sees a half-set-up
        # workspace.
        for path in created:
            try:
                if path.exists():
                    shutil.rmtree(path)
            except OSError as rm_exc:  # pragma: no cover — defensive
                logger.warning("Cleanup failed for %s: %s", path, rm_exc)
        try:
            if repos_root.exists() and not any(repos_root.iterdir()):
                repos_root.rmdir()
        except OSError:  # pragma: no cover — defensive
            pass
        raise
    return created


def cleanup_multi_repo_workspace(workspace: Path) -> None:
    """Remove ``workspace/repos`` if present (used between sequential tasks)."""
    repos_root = workspace / "repos"
    if not repos_root.exists():
        return
    try:
        shutil.rmtree(repos_root)
    except OSError as exc:
        logger.warning("Failed to remove %s: %s", repos_root, exc)


@runtime_checkable
class IsolationStrategy(Protocol):
    """Protocol for workspace isolation strategies."""

    def acquire(self) -> Path:
        """Get an isolated workspace path (blocks until one is available)."""
        ...

    def reset(self, workspace: Path) -> None:
        """Reset the workspace to a clean state."""
        ...

    def release(self, workspace: Path) -> None:
        """Return the workspace to the pool."""
        ...

    def cleanup(self) -> None:
        """Remove all managed workspaces."""
        ...


class WorktreeIsolation:
    """Manages a pool of git worktrees for parallel task execution.

    Creates N worktrees from the source repo.  ``acquire()`` blocks until a
    slot is free, ``release()`` resets and returns the slot to the pool.
    """

    def __init__(self, repo_path: Path, pool_size: int, namespace: str = "") -> None:
        if pool_size < 1:
            raise ValueError(f"pool_size must be >= 1, got {pool_size}")
        self._repo_path = repo_path.resolve()
        self._pool_size = pool_size
        base_name = ".codeprobe-worktrees"
        if namespace:
            base_name = f"{base_name}-{namespace}"
        self._base_dir = self._repo_path / base_name
        self._available: queue.Queue[Path] = queue.Queue()
        self._all_paths: list[Path] = []
        self._lock = threading.Lock()
        self._created = False

    def _create_pool(self) -> None:
        """Create the worktree pool (idempotent)."""
        with self._lock:
            if self._created:
                return
            # Prune stale worktree records left by previous interrupted runs
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=self._repo_path,
                capture_output=True,
            )
            self._base_dir.mkdir(parents=True, exist_ok=True)
            for i in range(self._pool_size):
                wt_path = self._base_dir / f"slot-{i}"
                if not wt_path.exists():
                    self._add_worktree(wt_path)
                self._all_paths.append(wt_path)
                self._available.put(wt_path)
            self._created = True

    def _add_worktree(self, wt_path: Path) -> None:
        """Add a detached worktree, recovering from stale git state."""
        try:
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt_path)],
                cwd=self._repo_path,
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError:
            # Stale record may remain even after prune — force-remove and retry
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(wt_path)],
                cwd=self._repo_path,
                capture_output=True,
            )
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=self._repo_path,
                capture_output=True,
            )
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt_path)],
                cwd=self._repo_path,
                check=True,
                capture_output=True,
            )

    def acquire(self) -> Path:
        """Get a worktree from the pool (blocks until available)."""
        if not self._created:
            self._create_pool()
        return self._available.get()

    def reset(self, workspace: Path) -> None:
        """Reset a worktree to clean state."""
        try:
            git_restore_clean(workspace)
        except subprocess.CalledProcessError as exc:
            logger.warning(
                "Worktree reset failed for %s (exit %d): %s",
                workspace,
                exc.returncode,
                exc.stderr.decode(errors="replace") if exc.stderr else "",
            )
        except OSError as exc:
            logger.warning("Worktree reset failed for %s: %s", workspace, exc)

    def release(self, workspace: Path) -> None:
        """Reset and return a worktree to the pool."""
        self.reset(workspace)
        self._available.put(workspace)

    def cleanup(self) -> None:
        """Remove all managed worktrees."""
        import shutil

        for wt_path in self._all_paths:
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=self._repo_path,
                    check=True,
                    capture_output=True,
                )
            except (subprocess.CalledProcessError, OSError):
                # Force-remove failed — delete the directory and let prune
                # clean up git's internal records.
                try:
                    if wt_path.exists():
                        shutil.rmtree(wt_path)
                except OSError as rm_exc:
                    logger.warning(
                        "Failed to remove worktree dir %s: %s", wt_path, rm_exc
                    )
        self._all_paths.clear()
        # Prune any stale records so future runs start clean
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=self._repo_path,
            capture_output=True,
        )
        # Clean up base directory if empty
        try:
            if self._base_dir.exists() and not any(self._base_dir.iterdir()):
                self._base_dir.rmdir()
        except OSError:
            pass
        self._created = False
