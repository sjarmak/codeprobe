"""Tests for codeprobe.trace.recorder — budgets, batching, truncate, seq."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codeprobe.trace.content_policy import ContentPolicy
from codeprobe.trace.recorder import (
    DEFAULT_TASK_BUDGET_BYTES,
    TraceBudgetExceededError,
    TraceOverflowPolicy,
    TraceRecorder,
)


def _count(db: Path) -> int:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()


def _rows(db: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db))
    try:
        return conn.execute(
            "SELECT config, task_id, event_seq, event_type, tool_name "
            "FROM events ORDER BY config, task_id, event_seq"
        ).fetchall()
    finally:
        conn.close()


@pytest.fixture
def null_policy() -> ContentPolicy:
    """Policy with no env values / globs — isolates recorder logic from redaction."""
    return ContentPolicy(env_values=frozenset(), deny_globs=())


@pytest.mark.unit
def test_record_single_event_flushes_at_batch_boundary(
    tmp_path: Path, null_policy: ContentPolicy
) -> None:
    db = tmp_path / "trace.db"
    with TraceRecorder(
        db, run_id="r", content_policy=null_policy, batch_size=1
    ) as rec:
        rec.record_event(
            config="c",
            task_id="t",
            event_type="tool_use",
            tool_name="Read",
            tool_input="foo",
        )
        # batch_size=1 flushes immediately
        assert _count(db) == 1


@pytest.mark.unit
def test_batch_flushed_on_close(tmp_path: Path, null_policy: ContentPolicy) -> None:
    db = tmp_path / "trace.db"
    rec = TraceRecorder(db, run_id="r", content_policy=null_policy, batch_size=10)
    for i in range(25):
        rec.record_event(
            config="c",
            task_id="t",
            event_type="tool_use",
            tool_name=f"Tool{i}",
        )
    # 20 of 25 should have been flushed (two batches of 10). Close flushes the remaining 5.
    rec.close()
    assert _count(db) == 25


@pytest.mark.unit
def test_task_budget_fail_loud(tmp_path: Path, null_policy: ContentPolicy) -> None:
    """Acceptance (6) — per-task >10MB by default causes fail-loud raise."""
    db = tmp_path / "trace.db"
    huge = "x" * 2048
    with TraceRecorder(
        db,
        run_id="r",
        content_policy=null_policy,
        task_budget_bytes=4096,  # small budget so test is fast
        batch_size=1,
    ) as rec:
        rec.record_event(
            config="c", task_id="t", event_type="tool_use", tool_output=huge
        )
        with pytest.raises(TraceBudgetExceededError) as excinfo:
            # second event pushes us over 4096
            rec.record_event(
                config="c", task_id="t", event_type="tool_use", tool_output=huge
            )
    assert excinfo.value.scope == "task"
    assert excinfo.value.task_id == "t"


@pytest.mark.unit
def test_run_budget_fail_loud(tmp_path: Path, null_policy: ContentPolicy) -> None:
    db = tmp_path / "trace.db"
    huge = "x" * 2048
    with TraceRecorder(
        db,
        run_id="r",
        content_policy=null_policy,
        task_budget_bytes=1_000_000,  # high task budget
        run_budget_bytes=4096,        # low run budget
        batch_size=1,
    ) as rec:
        rec.record_event(
            config="c", task_id="t1", event_type="tool_use", tool_output=huge
        )
        with pytest.raises(TraceBudgetExceededError) as excinfo:
            rec.record_event(
                config="c", task_id="t2", event_type="tool_use", tool_output=huge
            )
    assert excinfo.value.scope == "run"


@pytest.mark.unit
def test_run_budget_fails_even_with_truncate_policy(
    tmp_path: Path, null_policy: ContentPolicy
) -> None:
    """TRUNCATE applies only to per-task overflow; run overflow still raises."""
    db = tmp_path / "trace.db"
    huge = "x" * 2048
    with TraceRecorder(
        db,
        run_id="r",
        content_policy=null_policy,
        task_budget_bytes=1_000_000,
        run_budget_bytes=4096,
        overflow=TraceOverflowPolicy.TRUNCATE,
        batch_size=1,
    ) as rec:
        rec.record_event(
            config="c", task_id="t1", event_type="tool_use", tool_output=huge
        )
        with pytest.raises(TraceBudgetExceededError) as excinfo:
            rec.record_event(
                config="c", task_id="t2", event_type="tool_use", tool_output=huge
            )
    assert excinfo.value.scope == "run"


@pytest.mark.unit
def test_truncate_policy_writes_marker_and_stops_task(
    tmp_path: Path, null_policy: ContentPolicy
) -> None:
    """Acceptance (7) — trace_truncated row written; further events for task dropped;
    other tasks continue."""
    db = tmp_path / "trace.db"
    huge = "x" * 2048
    with TraceRecorder(
        db,
        run_id="r",
        content_policy=null_policy,
        task_budget_bytes=4096,
        run_budget_bytes=1_000_000,
        overflow=TraceOverflowPolicy.TRUNCATE,
        batch_size=1,
    ) as rec:
        rec.record_event(
            config="c", task_id="t1", event_type="tool_use", tool_output=huge
        )
        # Triggers overflow → truncate marker.
        rec.record_event(
            config="c", task_id="t1", event_type="tool_use", tool_output=huge
        )
        # Further events for t1 are silently dropped.
        rec.record_event(
            config="c", task_id="t1", event_type="tool_use", tool_name="ignored"
        )
        # Unrelated task still records.
        rec.record_event(
            config="c", task_id="t2", event_type="tool_use", tool_name="Grep"
        )

    rows = _rows(db)
    event_types = [r[3] for r in rows if r[1] == "t1"]
    assert "trace_truncated" in event_types
    # t1 should have exactly one real tool_use + one marker = 2 rows (dropped one never reached).
    t1_rows = [r for r in rows if r[1] == "t1"]
    assert len(t1_rows) == 2
    # t2 survived.
    t2_rows = [r for r in rows if r[1] == "t2"]
    assert len(t2_rows) == 1
    assert t2_rows[0][4] == "Grep"


@pytest.mark.unit
def test_event_seq_monotonic_per_task(
    tmp_path: Path, null_policy: ContentPolicy
) -> None:
    db = tmp_path / "trace.db"
    with TraceRecorder(
        db, run_id="r", content_policy=null_policy, batch_size=1
    ) as rec:
        for i in range(5):
            rec.record_event(config="c", task_id="ta", event_type="tool_use")
        for i in range(3):
            rec.record_event(config="c", task_id="tb", event_type="tool_use")

    conn = sqlite3.connect(str(db))
    try:
        ta = conn.execute(
            "SELECT event_seq FROM events WHERE task_id='ta' ORDER BY event_seq"
        ).fetchall()
        tb = conn.execute(
            "SELECT event_seq FROM events WHERE task_id='tb' ORDER BY event_seq"
        ).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in ta] == [0, 1, 2, 3, 4]
    assert [r[0] for r in tb] == [0, 1, 2]


@pytest.mark.unit
def test_close_is_idempotent(tmp_path: Path, null_policy: ContentPolicy) -> None:
    db = tmp_path / "trace.db"
    rec = TraceRecorder(db, run_id="r", content_policy=null_policy)
    rec.close()
    rec.close()  # must not raise


@pytest.mark.unit
def test_default_budgets_are_10mb_and_500mb(
    tmp_path: Path, null_policy: ContentPolicy
) -> None:
    """Acceptance (6) wiring — defaults match the PRD."""
    db = tmp_path / "trace.db"
    with TraceRecorder(db, run_id="r", content_policy=null_policy) as rec:
        assert rec._task_budget == DEFAULT_TASK_BUDGET_BYTES
        assert DEFAULT_TASK_BUDGET_BYTES == 10 * 1024 * 1024
        assert rec._run_budget == 500 * 1024 * 1024


@pytest.mark.unit
def test_ingest_stream_records_tool_use_and_result(
    tmp_path: Path, null_policy: ContentPolicy
) -> None:
    """Replaying a stream-json transcript produces one row per tool_use + one for result."""
    transcript = "\n".join(
        [
            '{"type": "assistant", "message": {"content": ['
            '{"type": "tool_use", "name": "Read", "input": {"path": "a.py"}}'
            "]}}",
            '{"type": "assistant", "message": {"content": ['
            '{"type": "tool_use", "name": "Grep", "input": {"pattern": "foo"}}'
            "]}}",
            '{"type": "result", "result": "done", '
            '"usage": {"input_tokens": 100, "output_tokens": 50}}',
        ]
    )
    db = tmp_path / "trace.db"
    with TraceRecorder(
        db, run_id="r", content_policy=null_policy, batch_size=1
    ) as rec:
        n = rec.ingest_stream(transcript, config="c", task_id="t")
    assert n == 3
    rows = _rows(db)
    assert [r[3] for r in rows] == ["tool_use", "tool_use", "result"]
    assert [r[4] for r in rows][:2] == ["Read", "Grep"]
