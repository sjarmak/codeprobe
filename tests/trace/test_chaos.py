"""Chaos tests — SIGKILL mid-write preserves committed rows.

Acceptance criterion (4): Already-committed rows survive a SIGKILL of
the writing process; the count before kill equals the count after
reopen.
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest


# Helper script executed in a child process. Uses batch_size=1 so every
# event is flushed immediately — maximises the number of rows committed
# before the kill lands.
_HELPER_SCRIPT = textwrap.dedent(
    """
    import sys
    import time
    from pathlib import Path

    from codeprobe.trace.content_policy import ContentPolicy
    from codeprobe.trace.recorder import TraceRecorder

    db_path = Path(sys.argv[1])
    n_events = int(sys.argv[2])

    # Explicit empty policy — isolate recorder behaviour from env scan.
    policy = ContentPolicy(env_values=frozenset(), deny_globs=())

    rec = TraceRecorder(
        db_path,
        run_id="chaos",
        content_policy=policy,
        batch_size=1,
    )
    for i in range(n_events):
        rec.record_event(
            config="c",
            task_id="t",
            event_type="tool_use",
            tool_name=f"Tool{i}",
            tool_input=f"input-{i}",
        )
        # Small sleep so the parent has time to observe the count grow
        # and send SIGKILL at a deterministic point.
        time.sleep(0.01)
    rec.close()
    """
).strip()


def _count(db: Path) -> int:
    conn = sqlite3.connect(str(db), timeout=10)
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


def _wait_for_min_rows(db: Path, target: int, timeout: float) -> int:
    """Poll until the DB has >= target rows or timeout expires."""
    deadline = time.monotonic() + timeout
    last = 0
    while time.monotonic() < deadline:
        try:
            last = _count(db)
        except sqlite3.OperationalError:
            # Locked or missing — retry.
            last = 0
        if last >= target:
            return last
        time.sleep(0.05)
    return last


@pytest.mark.integration
def test_sigkill_midwrite_preserves_committed_rows(tmp_path: Path) -> None:
    """Acceptance (4): count before kill == count after reopen."""
    db = tmp_path / "trace.db"
    helper = tmp_path / "helper.py"
    helper.write_text(_HELPER_SCRIPT, encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, str(helper), str(db), "200"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        # Wait until at least 5 rows are committed.
        committed_before_kill = _wait_for_min_rows(db, target=5, timeout=10.0)
        assert committed_before_kill >= 5, (
            f"helper never reached 5 rows; got {committed_before_kill}"
        )

        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        # After kill, no further rows can be committed — let file syncs settle.
        time.sleep(0.1)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    # Reopen the DB and verify ALL rows committed before the kill survived.
    # Count after reopen must be >= the pre-kill count (it may be higher
    # if additional rows committed between our last poll and the actual
    # kill, but it must never be lower).
    after = _count(db)
    assert after >= committed_before_kill, (
        f"rows lost after kill: before={committed_before_kill} after={after}"
    )
    # All surviving rows must be well-formed (self-consistent PK).
    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT event_seq FROM events ORDER BY event_seq"
        ).fetchall()
    finally:
        conn.close()
    seqs = [r[0] for r in rows]
    # Seqs are dense starting at 0 (recorder assigns 0,1,2,...).
    # SIGKILL may cut us at any point, so just check no duplicates.
    assert len(set(seqs)) == len(seqs), "duplicate event_seq after reopen"
