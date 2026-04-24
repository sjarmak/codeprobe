"""Tests for the tenant-scoped advisory file lock.

Covers PRD R4 acceptance criteria:

1. Two sequential calls succeed (the lock is released between invocations).
2. Two concurrent threads race — one wins, the other raises ``TENANT_IN_USE``.
3. The lockfile is cleaned up after successful release.
4. A stale lockfile (PID no longer running) is reclaimed without error.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from codeprobe.cli.errors import DiagnosticError
from codeprobe.paths import tenant_root
from codeprobe.tenant_lock import acquire_tenant_lock


@pytest.fixture(autouse=True)
def _tenant_state_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("CODEPROBE_STATE_ROOT", str(state))
    # Make sure the lock env-disable escape hatch is off for tests.
    monkeypatch.delenv("CODEPROBE_DISABLE_TENANT_LOCK", raising=False)
    return state


def test_sequential_calls_succeed() -> None:
    with acquire_tenant_lock("tenant-seq", "mine"):
        pass

    # Second call after the first releases should also succeed.
    with acquire_tenant_lock("tenant-seq", "mine"):
        pass


def test_concurrent_contention_raises_tenant_in_use() -> None:
    """Two threads race; one wins, the other raises TENANT_IN_USE."""
    start_barrier = threading.Barrier(2)
    winner_holding = threading.Event()
    loser_done = threading.Event()
    results: dict[str, object] = {}

    def winner() -> None:
        start_barrier.wait()
        with acquire_tenant_lock("tenant-race", "mine"):
            winner_holding.set()
            # Wait for loser to try and fail before releasing.
            loser_done.wait(timeout=5.0)
        results["winner"] = "ok"

    def loser() -> None:
        start_barrier.wait()
        # Let winner grab the lock first.
        winner_holding.wait(timeout=5.0)
        try:
            with acquire_tenant_lock("tenant-race", "mine"):
                results["loser"] = "unexpected-success"
        except DiagnosticError as exc:
            results["loser"] = exc
        finally:
            loser_done.set()

    t1 = threading.Thread(target=winner)
    t2 = threading.Thread(target=loser)
    t1.start()
    t2.start()
    t1.join(timeout=10.0)
    t2.join(timeout=10.0)

    assert results["winner"] == "ok"
    loser_result = results["loser"]
    assert isinstance(loser_result, DiagnosticError), (
        f"Expected DiagnosticError, got {loser_result!r}"
    )
    assert loser_result.code == "TENANT_IN_USE"
    assert loser_result.terminal is True
    # The error should name the winner's PID (which is this process's PID
    # since both threads share it).
    assert loser_result.detail["holder_pid"] == os.getpid()


def test_lockfile_cleaned_up_after_success() -> None:
    root = tenant_root("tenant-cleanup")
    lock_path = root / ".lock-mine"

    with acquire_tenant_lock("tenant-cleanup", "mine"):
        assert lock_path.exists(), (
            f"lockfile should exist while held, root={root}"
        )

    assert not lock_path.exists(), (
        "lockfile should be unlinked after successful release"
    )


def test_stale_lockfile_is_reclaimed() -> None:
    """If a lockfile records a dead PID, a fresh acquire must succeed."""
    root = tenant_root("tenant-stale")
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".lock-mine"

    # Find a PID that is guaranteed dead. Using a huge number sidesteps
    # the small chance of collision with a real short-lived PID.
    dead_pid = 2**22 + 1
    lock_path.write_text(str(dead_pid), encoding="utf-8")

    # Because the stale PID is only recorded in the file (not held via
    # flock on any kernel file description), acquire should succeed
    # immediately.
    with acquire_tenant_lock("tenant-stale", "mine") as acquired_path:
        assert acquired_path == lock_path
        assert lock_path.read_text(encoding="utf-8").strip() == str(os.getpid())


def test_different_commands_do_not_contend() -> None:
    """mine and run use different lock files and must not block each other."""
    with acquire_tenant_lock("tenant-xcmd", "mine"):
        # A separate command should acquire cleanly.
        with acquire_tenant_lock("tenant-xcmd", "run"):
            pass


def test_different_tenants_do_not_contend() -> None:
    with acquire_tenant_lock("tenant-a", "mine"):
        with acquire_tenant_lock("tenant-b", "mine"):
            pass


def test_disable_env_var_bypasses_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CODEPROBE_DISABLE_TENANT_LOCK=1 turns the context into a no-op."""
    monkeypatch.setenv("CODEPROBE_DISABLE_TENANT_LOCK", "1")
    # Two concurrent acquires in the same thread would deadlock on a real
    # non-reentrant lock; with the bypass they simply nest.
    with acquire_tenant_lock("tenant-bypass", "mine"):
        with acquire_tenant_lock("tenant-bypass", "mine"):
            pass
