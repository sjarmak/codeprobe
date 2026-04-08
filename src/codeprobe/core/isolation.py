"""Git worktree isolation for parallel task execution."""

from __future__ import annotations

import logging
import queue
import subprocess
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


def git_restore_clean(workdir: Path, *, extra_excludes: tuple[str, ...] = ()) -> None:
    """Restore tracked files and remove untracked files in *workdir*.

    Uses ``git restore .`` (tolerant of empty diffs) followed by
    ``git clean -fd``.  Always excludes ``.codeprobe`` and
    ``.codeprobe-worktrees``; pass *extra_excludes* for more.
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
        ".codeprobe-worktrees",
    ]
    for exc in extra_excludes:
        clean_cmd += ["-e", exc]
    subprocess.run(clean_cmd, cwd=workdir, check=True, capture_output=True)


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

    def __init__(self, repo_path: Path, pool_size: int) -> None:
        if pool_size < 1:
            raise ValueError(f"pool_size must be >= 1, got {pool_size}")
        self._repo_path = repo_path.resolve()
        self._pool_size = pool_size
        self._base_dir = self._repo_path / ".codeprobe-worktrees"
        self._available: queue.Queue[Path] = queue.Queue()
        self._all_paths: list[Path] = []
        self._lock = threading.Lock()
        self._created = False

    def _create_pool(self) -> None:
        """Create the worktree pool (idempotent)."""
        with self._lock:
            if self._created:
                return
            self._base_dir.mkdir(parents=True, exist_ok=True)
            for i in range(self._pool_size):
                wt_path = self._base_dir / f"slot-{i}"
                if not wt_path.exists():
                    subprocess.run(
                        ["git", "worktree", "add", "--detach", str(wt_path)],
                        cwd=self._repo_path,
                        check=True,
                        capture_output=True,
                    )
                self._all_paths.append(wt_path)
                self._available.put(wt_path)
            self._created = True

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
        for wt_path in self._all_paths:
            try:
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(wt_path)],
                    cwd=self._repo_path,
                    check=True,
                    capture_output=True,
                )
            except (subprocess.CalledProcessError, OSError) as exc:
                logger.warning("Failed to remove worktree %s: %s", wt_path, exc)
        self._all_paths.clear()
        # Clean up base directory if empty
        try:
            if self._base_dir.exists() and not any(self._base_dir.iterdir()):
                self._base_dir.rmdir()
        except OSError:
            pass
        self._created = False
