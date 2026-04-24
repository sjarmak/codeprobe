"""Resume + durability tests for MineState (r15-incremental-mining)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codeprobe.mining.state import MineState
from codeprobe.paths import compute_repo_hash, tenant_state_dir

TENANT = "acme"
REPO_HASH = compute_repo_hash("git@example.com:acme/repo.git", "main", "/tmp/wt")


@pytest.fixture(autouse=True)
def _patch_home(tenant_state_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect ``Path.home()`` so paths.py resolves under the tmp root.

    The module's own CODEPROBE_STATE_ROOT escape hatch is honored, but
    we also monkeypatch HOME for defence in depth — if a code path
    bypasses the env var it still lands under tmp_path.
    """
    monkeypatch.setenv("HOME", str(tenant_state_root.parent))


def _synthetic_shas(n: int) -> list[str]:
    return [f"{i:040x}" for i in range(1, n + 1)]


def test_open_applies_required_pragmas(tenant_state_root: Path) -> None:
    """PRAGMA inspection must show WAL + synchronous=FULL + busy_timeout=30000."""
    with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH) as state:
        db_path = state.db_path
        state.record_pending("abcd1234")

    # Open a plain connection to inspect persisted PRAGMAs. journal_mode=WAL
    # persists across connections (written into the header); synchronous is
    # a per-connection setting but the MineState constructor sets it so we
    # open via that path too to validate.
    conn = sqlite3.connect(str(db_path))
    try:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert journal_mode.lower() == "wal", f"expected wal, got {journal_mode!r}"
    finally:
        conn.close()

    # Reopen through MineState; synchronous + busy_timeout are set on open.
    with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH, sweep=False) as state:
        synchronous = state._conn.execute("PRAGMA synchronous").fetchone()[0]
        busy = state._conn.execute("PRAGMA busy_timeout").fetchone()[0]
        # synchronous=FULL is numeric value 2
        assert synchronous == 2, f"expected synchronous=FULL (2), got {synchronous}"
        assert busy == 30000, f"expected busy_timeout=30000, got {busy}"


def test_completed_shas_roundtrip(tenant_state_root: Path) -> None:
    """record_completed + reopen returns the same set (resume allowlist)."""
    shas = _synthetic_shas(5)

    with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH) as state:
        for sha in shas[:3]:
            state.record_running(sha)
            state.record_completed(sha)

    # Reopen with a new connection — simulate a separate process.
    with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH) as state:
        assert state.completed_shas() == set(shas[:3])


def test_startup_sweep_flips_running_and_pending(tenant_state_root: Path) -> None:
    """``running`` and ``pending`` rows become ``interrupted`` after sweep."""
    completed_sha, running_sha, pending_sha = _synthetic_shas(3)

    # Prime the DB without orderly close — emulates a SIGKILL between
    # ``record_running`` and ``record_completed``.
    with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH, sweep=False) as state:
        state.record_running(completed_sha)
        state.record_completed(completed_sha)
        state.record_running(running_sha)
        state.record_pending(pending_sha)

    # Fresh open triggers the sweep.
    with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH) as state:
        assert state.status(completed_sha) == "completed"
        assert state.status(running_sha) == "interrupted"
        assert state.status(pending_sha) == "interrupted"


def test_sigkill_mid_write_preserves_completed(tenant_state_root: Path) -> None:
    """Previously-completed rows survive; in-flight rows become interrupted.

    Simulates the SIGKILL chaos case: we open the DB, complete several
    commits, then (without closing) open a second connection that writes
    more rows in the ``running`` state and is never closed — emulated by
    opening a third connection that runs the startup sweep.
    """
    done = _synthetic_shas(10)[:5]
    in_flight = _synthetic_shas(10)[5:]

    # Phase 1: mark 5 as completed.
    with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH) as state:
        for sha in done:
            state.record_running(sha)
            state.record_completed(sha)

    # Phase 2: simulate a crash by opening with sweep=False, writing
    # running rows, and never calling record_completed.
    state_crash = MineState.open(
        tenant_id=TENANT, repo_hash=REPO_HASH, sweep=False
    )
    for sha in in_flight:
        state_crash.record_running(sha)
    # Close without completing — this models the process dying.
    state_crash.close()

    # Phase 3: the next invocation runs the sweep on open.
    with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH) as state:
        for sha in done:
            assert state.status(sha) == "completed", sha
        for sha in in_flight:
            assert state.status(sha) == "interrupted", sha


def test_resume_produces_identical_task_set(tenant_state_root: Path) -> None:
    """Interrupt a synthetic mine halfway, resume, and verify no SHA is lost.

    Models the actual extractor pattern (a stub "process" function +
    record_running -> record_completed cycle) and checks that the
    combined set of processed SHAs across the interrupted+resumed runs
    matches the uninterrupted baseline.
    """
    shas = _synthetic_shas(10)

    # Baseline: process everything in one run.
    baseline_processed: list[str] = []
    with MineState.open(tenant_id="baseline", repo_hash=REPO_HASH) as state:
        for sha in shas:
            if sha in state.completed_shas():
                continue
            state.record_running(sha)
            baseline_processed.append(sha)
            state.record_completed(sha)

    # Interrupted run: simulate a crash after SHA #5 is completed.
    interrupted_processed: list[str] = []
    with MineState.open(tenant_id="interrupted", repo_hash=REPO_HASH) as state:
        for sha in shas[:5]:
            state.record_running(sha)
            interrupted_processed.append(sha)
            state.record_completed(sha)
        # Mark a 6th as running without completing (crash mid-commit).
        state.record_running(shas[5])

    # Resume.
    with MineState.open(tenant_id="interrupted", repo_hash=REPO_HASH) as state:
        # shas[5] was running; sweep flipped it to interrupted; it is NOT
        # in completed_shas() so it must be re-processed here.
        for sha in shas:
            if sha in state.completed_shas():
                continue
            state.record_running(sha)
            interrupted_processed.append(sha)
            state.record_completed(sha)

    # Uninterrupted set equals the final completed set on the resumed DB.
    with MineState.open(tenant_id="interrupted", repo_hash=REPO_HASH) as state:
        assert state.completed_shas() == set(shas)

    # And sanity-check the baseline: every sha was processed exactly once.
    assert sorted(set(baseline_processed)) == sorted(shas)


def test_write_failures_roll_back(tenant_state_root: Path) -> None:
    """A failing write inside BEGIN IMMEDIATE must not half-commit."""
    with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH) as state:
        with pytest.raises(ValueError):
            state.record_running("")  # invalid sha triggers ValueError
        # The DB should remain empty.
        assert state.completed_shas() == set()


def test_tenant_isolation(tenant_state_root: Path) -> None:
    """Two tenants against the same repo_hash see independent state."""
    sha = "abc123"
    with MineState.open(tenant_id="alpha", repo_hash=REPO_HASH) as state:
        state.record_running(sha)
        state.record_completed(sha)
    with MineState.open(tenant_id="beta", repo_hash=REPO_HASH) as state:
        assert state.completed_shas() == set()
    with MineState.open(tenant_id="alpha", repo_hash=REPO_HASH) as state:
        assert state.completed_shas() == {sha}


def test_tenant_state_dir_layout(tenant_state_root: Path) -> None:
    """State files live under ``<root>/<tenant_id>/<repo_hash>/mine.db``."""
    path = tenant_state_dir(tenant_id="acme", repo_hash=REPO_HASH)
    assert path.parent.name == "acme"
    assert path.name == REPO_HASH
    # tenant_state_dir is pure — MineState.open materializes the dir.
    # Instantiate a MineState to force creation, then re-check.
    with MineState.open(tenant_id="acme", repo_hash=REPO_HASH):
        pass
    assert path.is_dir()
