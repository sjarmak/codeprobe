"""Parallel-writer integration tests for MineState + worktree flock.

Scaled to 20 worktrees rather than 200 because the acceptance criterion
explicitly allows a smaller count and 200 would push the test past the
default pytest timeout for little extra coverage. The invariant checked
— ``PRAGMA integrity_check`` returning ``ok`` after concurrent writers
— holds identically at either scale.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from codeprobe.mining import state as state_module
from codeprobe.mining.state import MineState
from codeprobe.paths import compute_repo_hash, tenant_state_dir

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


def test_open_converts_to_wal_while_another_writer_holds_the_db(
    tenant_state_root: Path,
) -> None:
    """A not-yet-WAL mine.db still converts when a writer holds the database.

    Regression test for codeprobe-uhr4, the defect behind the intermittent
    ``test_parallel_workers_preserve_integrity`` failures. Moving a database
    onto WAL rewrites its header under a brief exclusive lock, and SQLite
    reports that contention as ``SQLITE_BUSY`` *without* consulting the busy
    handler — so ``MineState.open`` used to die on the spot with ``database is
    locked`` whenever a sibling worker was mid-write during the conversion.
    ``MineState`` now waits the writer out itself.
    """
    state_dir = tenant_state_dir(tenant_id="convert", repo_hash=REPO_HASH)
    state_dir.mkdir(parents=True, exist_ok=True)

    # Seed a database in the default rollback-journal mode, so open() faces a
    # real conversion rather than the no-op it gets on an already-WAL file.
    seed = sqlite3.connect(
        str(state_dir / "mine.db"), isolation_level=None, check_same_thread=False
    )
    holding = threading.Event()
    hold_errors: list[BaseException] = []

    def _hold_write_lock() -> None:
        """Hold an exclusive write transaction, then release it."""
        try:
            seed.execute("BEGIN IMMEDIATE")
            seed.execute("INSERT OR IGNORE INTO seeded (k) VALUES ('x')")
            holding.set()
            time.sleep(0.3)
            seed.execute("COMMIT")
        except BaseException as exc:  # noqa: BLE001 - surface anything
            hold_errors.append(exc)
            holding.set()

    try:
        seed.execute("PRAGMA journal_mode=DELETE")
        seed.execute("CREATE TABLE seeded (k TEXT PRIMARY KEY)")
        assert (
            seed.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        ), "seed db must start outside WAL for the conversion to be exercised"

        writer = threading.Thread(target=_hold_write_lock, name="holder")
        writer.start()
        assert holding.wait(timeout=5), "writer never took the lock"

        with MineState.open(
            tenant_id="convert", repo_hash=REPO_HASH, sweep=False
        ) as state:
            mode = state._conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal", f"expected wal, got {mode!r}"
            # And the connection is usable — the conversion left no debris.
            state.record_completed("f" * 40)
            assert state.completed_shas() == {"f" * 40}

        writer.join(timeout=5)
        assert not writer.is_alive(), "writer did not finish"
        assert not hold_errors, f"writer errors: {hold_errors!r}"
    finally:
        seed.close()


def test_open_raises_when_wal_conversion_budget_is_exhausted(
    tenant_state_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A writer that never lets go makes ``open`` give up loudly, not silently.

    The companion test above proves the retry loop waits a *transient* writer
    out. This one proves the other end of the contract: when the busy-timeout
    budget runs out with the database still held, ``_enable_wal`` raises rather
    than handing back a connection on a rollback journal, and it says so in the
    log.

    Nothing here races. The write lock is taken before ``open`` is called and
    released only in the ``finally``, so the conversion cannot succeed at any
    point during the call — the shrunken deadline is the only thing that can
    expire.
    """
    budget_ms = 200
    monkeypatch.setattr(state_module, "_BUSY_TIMEOUT_MS", budget_ms)
    caplog.set_level(logging.DEBUG, logger="codeprobe.mining.state")

    state_dir = tenant_state_dir(tenant_id="exhausted", repo_hash=REPO_HASH)
    state_dir.mkdir(parents=True, exist_ok=True)

    seed = sqlite3.connect(
        str(state_dir / "mine.db"), isolation_level=None, check_same_thread=False
    )
    # open() is the only owner of the connection it makes, so a failed open
    # has to close it — nothing downstream ever will.
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def _spy_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn: sqlite3.Connection = real_connect(*args, **kwargs)
        opened.append(conn)
        return conn

    try:
        seed.execute("PRAGMA journal_mode=DELETE")
        seed.execute("CREATE TABLE seeded (k TEXT PRIMARY KEY)")
        # Hold the RESERVED lock for the whole of open(). A blocked WAL
        # conversion reports SQLITE_BUSY immediately instead of consulting the
        # busy handler, so every retry fails on contact until the deadline.
        seed.execute("BEGIN IMMEDIATE")
        seed.execute("INSERT INTO seeded (k) VALUES ('x')")

        monkeypatch.setattr(sqlite3, "connect", _spy_connect)
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            MineState.open(tenant_id="exhausted", repo_hash=REPO_HASH, sweep=False)
    finally:
        seed.execute("ROLLBACK")
        seed.close()

    assert f"could not enable WAL journal mode within {budget_ms}ms" in str(
        excinfo.value
    )

    # The SQLITE_BUSY that actually blocked the conversion is chained, so the
    # traceback names the real cause instead of only our summary.
    cause = excinfo.value.__cause__
    assert isinstance(cause, sqlite3.OperationalError), f"no chained cause: {cause!r}"
    assert "locked" in str(cause), f"unexpected cause: {cause!r}"

    # The failed open owns the only reference to its connection, so it has to
    # close it on the way out or the handle leaks.
    assert len(opened) == 1, f"expected open() to make one connection, got {opened!r}"
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")

    records = [r for r in caplog.records if r.name == "codeprobe.mining.state"]
    warnings = [r for r in records if r.levelno == logging.WARNING]
    debugs = [r for r in records if r.levelno == logging.DEBUG]
    assert len(warnings) == 1, f"expected one give-up warning, got {warnings!r}"
    assert "locked" in warnings[0].getMessage()
    assert debugs, "expected at least one debug record for a backoff retry"

    # Attempt count and elapsed time are what make the warning actionable; they
    # ride on the record as ``extra`` fields so a structured handler can read
    # them without parsing the message.
    attempts = getattr(warnings[0], "wal_attempts")
    elapsed_s = getattr(warnings[0], "wal_elapsed_s")
    assert attempts == len(debugs) + 1, (
        f"warning reports {attempts} attempts but {len(debugs)} were logged as retries"
    )
    assert elapsed_s >= budget_ms / 1000.0


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
