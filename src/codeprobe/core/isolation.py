"""Git worktree isolation for parallel task execution."""

from __future__ import annotations

import contextlib
import logging
import queue
import shutil
import subprocess
import threading
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, runtime_checkable

from codeprobe.models.task import RepoRef

logger = logging.getLogger(__name__)


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
