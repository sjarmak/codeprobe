"""Tests for codeprobe-qn2f: a worktree whose reset fails is quarantined.

Before this, ``WorktreeIsolation.reset`` logged the failure and ``release``
requeued the slot anyway, so a later trial could inherit the previous agent's
source edits, generated answers, or staged files and be scored against them.
"""

from __future__ import annotations

import queue
import stat
import subprocess
import threading
from pathlib import Path

import pytest

from codeprobe.adapters.protocol import AgentConfig
from codeprobe.analysis.validity import TrialClass, classify_trial
from codeprobe.cli.run_cmd import build_run_envelope_summary
from codeprobe.core.executor import execute_config
from codeprobe.core.isolation import (
    WorktreeIsolation,
    WorktreePoolExhaustedError,
    WorktreeResetError,
    git_restore_clean,
)
from codeprobe.models.experiment import ExperimentConfig
from tests.conftest import FakeAdapter


def _make_repo(base: Path, name: str = "repo") -> Path:
    """Create a git repo with one committed file."""
    repo = base / name
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    (repo / "src.py").write_text("original\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _make_task_dir(base: Path, name: str) -> Path:
    """Create a minimal passing task directory."""
    task_dir = base / name
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("Fix the bug.")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    test_sh = tests_dir / "test.sh"
    test_sh.write_text("#!/bin/bash\nexit 0\n")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)
    return task_dir


class _FailingResetIsolation(WorktreeIsolation):
    """Real pool whose reset fails once ``fail_next`` is set."""

    def __init__(self, repo_path: Path, pool_size: int) -> None:
        super().__init__(repo_path, pool_size=pool_size, namespace="qn2f")
        self.fail_next = True
        self._fail_lock = threading.Lock()

    def reset(self, workspace: Path) -> None:
        with self._fail_lock:
            should_fail = self.fail_next
            self.fail_next = False
        if should_fail:
            raise WorktreeResetError(
                workspace, subprocess.CalledProcessError(1, ["git", "clean"])
            )
        super().reset(workspace)


class TestResetRaises:
    """reset() surfaces failure instead of logging and swallowing it."""

    @pytest.mark.parametrize(
        "cause",
        [
            subprocess.CalledProcessError(1, ["git", "clean", "-fd"]),
            OSError("disk gone"),
        ],
        ids=["called-process-error", "oserror"],
    )
    def test_reset_wraps_git_failure(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cause: Exception,
    ) -> None:
        def _boom(workdir: Path, **kwargs: object) -> None:
            raise cause

        monkeypatch.setattr("codeprobe.core.isolation.git_restore_clean", _boom)
        pool = WorktreeIsolation(tmp_path, pool_size=1, namespace="qn2f")
        slot = tmp_path / "slot-0"

        with pytest.raises(WorktreeResetError) as excinfo:
            pool.reset(slot)
        assert excinfo.value.workspace == slot


class TestFailedRestoreIsNotSilentSuccess:
    """A failed ``git restore`` must not read as a clean reset.

    ``git clean`` only removes UNTRACKED files, so a swallowed ``git restore``
    failure left the previous agent's edits sitting in TRACKED files while
    ``reset()`` reported success — the slot went straight back into the pool
    carrying exactly the source edits this bead is about.
    """

    def _worktree_git_dir(self, wt: Path) -> Path:
        """Resolve a worktree's real git dir (``.git`` is a file there)."""
        dot_git = wt / ".git"
        if dot_git.is_file():
            gitdir = dot_git.read_text().split("gitdir:")[1].strip()
            return Path(gitdir)
        return dot_git

    def test_failed_restore_quarantines_instead_of_requeueing(
        self, tmp_path: Path
    ) -> None:
        repo = _make_repo(tmp_path)
        pool = WorktreeIsolation(repo, pool_size=1, namespace="qn2f")
        try:
            wt = pool.acquire()
            (wt / "src.py").write_text("edited by trial one\n")
            # An index lock makes `git restore` fail while `git clean` succeeds.
            lock = self._worktree_git_dir(wt) / "index.lock"
            lock.write_text("")
            try:
                with pytest.raises(WorktreeResetError):
                    pool.release(wt)
            finally:
                lock.unlink(missing_ok=True)

            assert wt in pool.quarantined
            # The tracked edit really did survive — proving the reset failure
            # was substantive and not a spurious exit code.
            assert (wt / "src.py").read_text().strip() == "edited by trial one"
        finally:
            pool.cleanup()

    def test_empty_worktree_head_is_tolerated(self, tmp_path: Path) -> None:
        """A repo with no commits is clean, not dirty — must not raise.

        ``git restore`` reports "could not resolve HEAD" there; nothing is
        tracked, so there is nothing to contaminate.
        """
        empty = tmp_path / "empty"
        empty.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=empty, check=True)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=empty, check=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=empty, check=True)

        git_restore_clean(empty)  # must not raise


class TestQuarantine:
    """A failed reset retires the slot instead of requeueing it."""

    def test_failed_release_does_not_requeue(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        pool = _FailingResetIsolation(repo, pool_size=2)
        try:
            dirty = pool.acquire()
            with pytest.raises(WorktreeResetError):
                pool.release(dirty)

            assert dirty in pool.quarantined
            # The one surviving slot is still usable and is never the dirty one.
            clean = pool.acquire()
            assert clean != dirty
            pool.release(clean)
            assert pool.acquire() != dirty
        finally:
            pool.cleanup()

    def test_acquire_raises_when_all_slots_quarantined(self, tmp_path: Path) -> None:
        """A fully quarantined pool raises rather than blocking forever."""
        repo = _make_repo(tmp_path)
        pool = _FailingResetIsolation(repo, pool_size=1)
        try:
            dirty = pool.acquire()
            with pytest.raises(WorktreeResetError):
                pool.release(dirty)

            with pytest.raises(WorktreePoolExhaustedError):
                pool.acquire()
            # The poison pill persists, so every later waiter wakes too.
            with pytest.raises(WorktreePoolExhaustedError):
                pool.acquire()
        finally:
            pool.cleanup()

    def test_exhausted_pool_wakes_a_blocked_acquirer(self, tmp_path: Path) -> None:
        """A thread already blocked in acquire() wakes when the pool dies."""
        repo = _make_repo(tmp_path)
        pool = _FailingResetIsolation(repo, pool_size=1)
        outcome: queue.Queue[BaseException | Path] = queue.Queue()

        def _waiter() -> None:
            try:
                outcome.put(pool.acquire())
            except BaseException as exc:  # noqa: BLE001 — reported to main thread
                outcome.put(exc)

        try:
            dirty = pool.acquire()
            thread = threading.Thread(target=_waiter, daemon=True)
            thread.start()
            with pytest.raises(WorktreeResetError):
                pool.release(dirty)

            thread.join(timeout=10)
            assert not thread.is_alive(), "acquire() hung on an exhausted pool"
            assert isinstance(outcome.get_nowait(), WorktreePoolExhaustedError)
        finally:
            pool.cleanup()


class TestPoolAccounting:
    """The quarantine bookkeeping must not leak across life-cycles."""

    def test_cleanup_clears_quarantine_state(self, tmp_path: Path) -> None:
        """A rebuilt pool starts clean instead of inheriting a poison pill."""
        repo = _make_repo(tmp_path)
        pool = _FailingResetIsolation(repo, pool_size=1)
        try:
            dirty = pool.acquire()
            with pytest.raises(WorktreeResetError):
                pool.release(dirty)
            pool.cleanup()

            assert pool.quarantined == ()
            # The rebuilt pool is healthy — acquire() must not raise.
            revived = pool.acquire()
            assert revived.exists()
            pool.release(revived)
        finally:
            pool.cleanup()

    def test_unexpected_reset_error_still_quarantines(self, tmp_path: Path) -> None:
        """Any reset failure decrements capacity, not just WorktreeResetError.

        A slot that is neither requeued nor quarantined would shrink the pool
        without ever letting it reach the exhausted state, hanging every
        blocked acquirer.
        """
        repo = _make_repo(tmp_path)

        class _OddFailure(WorktreeIsolation):
            def reset(self, workspace: Path) -> None:
                raise ValueError("something unforeseen")

        pool = _OddFailure(repo, pool_size=1, namespace="qn2f")
        try:
            wt = pool.acquire()
            with pytest.raises(ValueError):
                pool.release(wt)

            assert wt in pool.quarantined
            with pytest.raises(WorktreePoolExhaustedError):
                pool.acquire()
        finally:
            pool.cleanup()


class TestTwoTrialRegression:
    """End-to-end: trial one contaminates a slot, trial two can't inherit it."""

    def test_contaminated_slot_never_reaches_a_later_trial(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Trial one's slot is retired, and the halt stays visible.

        The trials the dead pool killed must read as infra failures. A halt
        that merely stopped collecting results left the envelope reporting a
        smaller, apparently healthy run — the reward mean computed over the
        survivors with nothing marking what was lost.
        """
        repo = _make_repo(tmp_path)
        tasks = [_make_task_dir(tmp_path, f"task-{i:03d}") for i in range(3)]
        pool = _FailingResetIsolation(repo, pool_size=1)

        try:
            results = execute_config(
                adapter=FakeAdapter(stdout="output"),
                task_dirs=tasks,
                repo_path=repo,
                experiment_config=ExperimentConfig(label="baseline"),
                agent_config=AgentConfig(),
                parallel=3,
                isolation=pool,
            )
            # Read before cleanup(), which resets quarantine bookkeeping.
            quarantined = pool.quarantined
        finally:
            pool.cleanup()

        # The slot that failed to reset was retired, not reused.
        assert len(quarantined) == 1

        # Trial one's output survived the quarantine, carrying the reason.
        quarantine_notes = [
            r.metadata.get("isolation_reset_failed")
            for r in results
            if r.metadata.get("isolation_reset_failed")
        ]
        assert quarantine_notes, "the failed reset was not recorded on the trial"
        assert "Worktree reset failed" in quarantine_notes[0]
        assert "Worktree reset failed" in capsys.readouterr().err

        # Every dispatched trial is accounted for — none silently dropped, and
        # the ones the dead pool killed are honest infra failures rather than
        # genuine agent failures or silent zeros scored in a dirty workspace.
        assert len(results) == len(tasks)
        classes = [classify_trial(r) for r in results]
        assert classes.count(TrialClass.VALID) == 1, "the good trial was lost"
        assert classes.count(TrialClass.INFRA_FAILURE) == 2, (
            "trials killed by the dead pool must read as infra failures, "
            "not as genuine agent failures or missing rows"
        )

        summary = build_run_envelope_summary({"baseline": results})[0][0]
        assert summary["tasks"] == 3
        assert summary["infra_failure_count"] == 2
        assert summary["scored_count"] == 1
