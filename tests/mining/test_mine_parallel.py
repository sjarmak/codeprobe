"""Parallel-writer integration tests for MineState + worktree flock.

Scaled to 20 worktrees rather than 200 because the acceptance criterion
explicitly allows a smaller count and 200 would push the test past the
default pytest timeout for little extra coverage. The invariant checked
— ``PRAGMA integrity_check`` returning ``ok`` after concurrent writers
— holds identically at either scale.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from codeprobe.mining.state import MineState
from codeprobe.paths import compute_repo_hash

REPO_HASH = compute_repo_hash("git@example.com:p/r.git", "main", "/tmp/p")
NUM_WORKERS = 20


@pytest.fixture(autouse=True)
def _patch_home(tenant_state_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tenant_state_root.parent))


def _worker(
    idx: int,
    tenant_id: str,
    git_dir: Path,
    barrier: threading.Barrier,
    errors: list[BaseException],
) -> None:
    """One worker: acquire flock, write two state rows, release."""
    try:
        barrier.wait()
        state = MineState.open(tenant_id=tenant_id, repo_hash=REPO_HASH, sweep=False)
        try:
            sha = f"{idx:040x}"
            with state.worktree_lock(git_dir):
                # Emulate the worktree-create + process + worktree-remove
                # critical section by writing state transitions inside the
                # exclusive lock.
                wt_dir = git_dir.parent / f"worktree-{idx}"
                wt_dir.mkdir(parents=True, exist_ok=True)
                state.record_running(sha)
                state.record_completed(sha)
                # Remove the worktree dir — mirror the caller's lifecycle.
                for p in wt_dir.iterdir():
                    p.unlink()
                wt_dir.rmdir()
        finally:
            state.close()
    except BaseException as exc:  # noqa: BLE001 - want to surface anything
        errors.append(exc)


def test_parallel_workers_preserve_integrity(
    tenant_state_root: Path, tmp_path: Path
) -> None:
    """20 parallel workers create/remove worktrees without corrupting the DB."""
    tenant_id = "parallel"
    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)

    barrier = threading.Barrier(NUM_WORKERS)
    errors: list[BaseException] = []
    threads = [
        threading.Thread(
            target=_worker,
            args=(i, tenant_id, git_dir, barrier, errors),
            name=f"mine-worker-{i}",
        )
        for i in range(NUM_WORKERS)
    ]

    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), f"{t.name} did not finish"

    assert not errors, f"worker errors: {errors!r}"

    # Reopen without sweep so we can assert on raw final state.
    with MineState.open(
        tenant_id=tenant_id, repo_hash=REPO_HASH, sweep=False
    ) as state:
        completed = state.completed_shas()
        # Every worker wrote one completed SHA.
        assert len(completed) == NUM_WORKERS, (
            f"expected {NUM_WORKERS} completed, got {len(completed)}"
        )

        # Integrity check — acceptance criterion #5.
        integrity = state._conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok", f"integrity_check returned {integrity!r}"


def test_worktree_lock_is_exclusive(
    tenant_state_root: Path, tmp_path: Path
) -> None:
    """Only one thread at a time may hold the worktree lock."""
    git_dir = tmp_path / "repo" / ".git"
    git_dir.mkdir(parents=True, exist_ok=True)

    state = MineState.open(tenant_id="excl", repo_hash=REPO_HASH)

    enter = threading.Event()
    proceed = threading.Event()
    observed: list[str] = []

    def first() -> None:
        with state.worktree_lock(git_dir):
            observed.append("first-in")
            enter.set()
            proceed.wait(timeout=5)
            observed.append("first-out")

    def second() -> None:
        # Wait until `first` is inside the lock.
        enter.wait(timeout=5)
        # Open a second state so flock's fd-ownership semantics match a
        # real second process rather than a reentrant lock on the same fd.
        state2 = MineState.open(
            tenant_id="excl", repo_hash=REPO_HASH, sweep=False
        )
        try:
            with state2.worktree_lock(git_dir):
                observed.append("second-in")
                observed.append("second-out")
        finally:
            state2.close()

    t1 = threading.Thread(target=first)
    t2 = threading.Thread(target=second)
    t1.start()
    t2.start()

    # Let `first` hold the lock briefly, then release.
    assert enter.wait(timeout=5)
    # `second` should be blocked until we signal `first` to exit.
    # We cannot deterministically assert it's blocked without a sleep,
    # so instead verify the observed ordering after both finish.
    proceed.set()

    t1.join(timeout=5)
    t2.join(timeout=5)
    state.close()

    # Ordering: first-in, first-out must both precede second-in.
    assert observed.index("first-out") < observed.index("second-in")
    # And second-out is last.
    assert observed[-1] == "second-out"


def test_integrity_check_against_crashed_writer(
    tenant_state_root: Path,
) -> None:
    """A writer that crashes mid-transaction leaves an intact DB.

    Uses the ``_write`` context manager's ROLLBACK path to simulate the
    crash; afterwards PRAGMA integrity_check must report ``ok`` and the
    crashed row must not appear as completed.
    """
    with MineState.open(tenant_id="crash", repo_hash=REPO_HASH) as state:
        sha = "deadbeef"
        # Good write first so we know the DB is usable afterwards.
        state.record_running(sha)
        state.record_completed(sha)

        # Simulate a mid-write crash via an exception inside ``_write``.
        with pytest.raises(ValueError, match="synthetic"):
            with state._write():
                state._conn.execute(
                    "INSERT OR REPLACE INTO commits (sha, status) VALUES (?, 'running')",
                    ("crash-sha",),
                )
                raise ValueError("synthetic crash")

        integrity = state._conn.execute("PRAGMA integrity_check").fetchone()[0]
        assert integrity == "ok"
        # The crashed insert must have rolled back.
        assert state.status("crash-sha") is None
        assert state.status(sha) == "completed"
