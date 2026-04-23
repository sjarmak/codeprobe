"""Streaming event recorder with write-side budget enforcement.

The recorder is the only writer to the trace DB. It enforces two
budgets at write time (INV1 — fail-loud):

* **per-task** (default 10 MB) — resets when a new ``task_id`` starts
* **per-run**  (default 500 MB) — accumulates across all tasks in the
  recorder's lifetime

On overflow the recorder raises :class:`TraceBudgetExceeded`. The
opt-in ``--trace-overflow=truncate`` policy writes a final
``event_type='trace_truncated'`` row for the affected task and silently
drops subsequent events for that task (without halting the run).
"""

from __future__ import annotations

import enum
import json
import logging
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeprobe.trace.content_policy import ContentPolicy
from codeprobe.trace.store import open_store

logger = logging.getLogger(__name__)


# Default budgets — per PRD: per-task 10 MB, per-run 500 MB.
DEFAULT_TASK_BUDGET_BYTES = 10 * 1024 * 1024
DEFAULT_RUN_BUDGET_BYTES = 500 * 1024 * 1024

# Batch-flush threshold. Every N events we issue BEGIN IMMEDIATE /
# COMMIT. Smaller = lower latency-to-disk (better crash recovery);
# larger = fewer fsyncs (higher throughput). 10 is the PRD default.
DEFAULT_BATCH_SIZE = 10

# Per-row structural overhead (rowid + integer columns + fsync bookkeeping).
# Not a tight upper bound — just a reasonable constant so the accumulator
# reflects real disk pressure rather than just string-field lengths.
_ROW_OVERHEAD_BYTES = 64


class TraceOverflowPolicy(enum.Enum):
    """Behaviour when a budget is exceeded."""

    FAIL = "fail"
    TRUNCATE = "truncate"


class TraceBudgetExceeded(Exception):
    """Raised when a write would exceed per-task or per-run byte budget.

    ``scope`` is ``'task'`` or ``'run'``. ``limit`` / ``current`` are
    the configured cap and the accumulator value BEFORE this write
    would have been applied.
    """

    def __init__(
        self,
        *,
        scope: str,
        config: str,
        task_id: str,
        limit: int,
        current: int,
        incoming: int,
    ) -> None:
        self.scope = scope
        self.config = config
        self.task_id = task_id
        self.limit = limit
        self.current = current
        self.incoming = incoming
        super().__init__(
            f"Trace {scope} budget exceeded for config={config!r} "
            f"task_id={task_id!r}: current={current} + incoming={incoming} "
            f"would exceed limit={limit}"
        )


@dataclass(frozen=True)
class _PendingRow:
    run_id: str
    config: str
    task_id: str
    event_seq: int
    ts: float
    event_type: str
    tool_name: str | None
    tool_input: str | None
    tool_output: str | None
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    bytes_written: int


class TraceRecorder:
    """Batched, budget-enforcing writer for the events table.

    Usage::

        with TraceRecorder(db_path, run_id="exp1") as rec:
            rec.record_event(
                config="mcp", task_id="t-0001",
                event_type="tool_use", tool_name="Read",
                tool_input="...", tool_output="...",
            )

    The context manager flushes pending rows on exit. If a budget is
    exceeded, the exception propagates out — use ``overflow=TRUNCATE``
    to trade fail-loud for partial traces.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        run_id: str,
        task_budget_bytes: int = DEFAULT_TASK_BUDGET_BYTES,
        run_budget_bytes: int = DEFAULT_RUN_BUDGET_BYTES,
        overflow: TraceOverflowPolicy = TraceOverflowPolicy.FAIL,
        content_policy: ContentPolicy | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if task_budget_bytes <= 0:
            raise ValueError("task_budget_bytes must be positive")
        if run_budget_bytes <= 0:
            raise ValueError("run_budget_bytes must be positive")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")

        self._db_path = db_path
        self._run_id = run_id
        self._task_budget = task_budget_bytes
        self._run_budget = run_budget_bytes
        self._overflow = overflow
        self._policy = content_policy if content_policy is not None else ContentPolicy()
        self._batch_size = batch_size

        self._conn: sqlite3.Connection | None = open_store(db_path)
        self._lock = threading.Lock()

        self._task_bytes: dict[tuple[str, str], int] = {}
        self._run_bytes: int = 0
        self._seq: dict[tuple[str, str], int] = {}
        self._truncated: set[tuple[str, str]] = set()
        self._pending: list[_PendingRow] = []

    # ------------------------------------------------------------------
    # Context manager plumbing
    # ------------------------------------------------------------------

    def __enter__(self) -> TraceRecorder:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_event(
        self,
        *,
        config: str,
        task_id: str,
        event_type: str,
        tool_name: str | None = None,
        tool_input: str | None = None,
        tool_output: str | None = None,
        duration_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        ts: float | None = None,
    ) -> None:
        """Record one event, applying redaction and budget checks.

        Raises :class:`TraceBudgetExceeded` when the write would exceed
        per-task or per-run budget under ``overflow=FAIL``. Under
        ``TRUNCATE``, per-task overflow writes a final
        ``trace_truncated`` row and silently drops subsequent events
        for that task (run continues).
        """
        if self._conn is None:
            raise RuntimeError("TraceRecorder is closed")

        key = (config, task_id)

        with self._lock:
            if key in self._truncated:
                return

            # Redaction happens BEFORE byte counting so budgets reflect
            # what actually gets persisted.
            r_input = self._policy.apply(tool_input, is_output=False)
            r_output = self._policy.apply(tool_output, is_output=True)

            incoming = self._row_bytes(
                event_type=event_type,
                tool_name=tool_name,
                tool_input=r_input,
                tool_output=r_output,
            )

            current_task = self._task_bytes.get(key, 0)
            task_overflow = current_task + incoming > self._task_budget
            run_overflow = self._run_bytes + incoming > self._run_budget

            if task_overflow or run_overflow:
                self._handle_overflow(
                    config=config,
                    task_id=task_id,
                    incoming=incoming,
                    task_overflow=task_overflow,
                    run_overflow=run_overflow,
                    current_task=current_task,
                )
                return  # only reached under TRUNCATE (task-scope)

            seq = self._seq.get(key, 0)
            self._seq[key] = seq + 1

            row = _PendingRow(
                run_id=self._run_id,
                config=config,
                task_id=task_id,
                event_seq=seq,
                ts=ts if ts is not None else time.time(),
                event_type=event_type,
                tool_name=tool_name,
                tool_input=r_input,
                tool_output=r_output,
                duration_ms=duration_ms,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                bytes_written=incoming,
            )
            self._pending.append(row)
            self._task_bytes[key] = current_task + incoming
            self._run_bytes += incoming

            if len(self._pending) >= self._batch_size:
                self._flush_locked()

    def flush(self) -> None:
        """Force a flush of buffered rows to disk."""
        with self._lock:
            self._flush_locked()

    def close(self) -> None:
        """Flush pending rows and close the underlying connection."""
        with self._lock:
            if self._conn is None:
                return
            try:
                self._flush_locked()
            finally:
                self._conn.close()
                self._conn = None

    def ingest_stream(
        self,
        stdout: str,
        *,
        config: str,
        task_id: str,
    ) -> int:
        """Replay a Claude CLI ``--output-format stream-json`` transcript.

        Each ``assistant`` message's ``tool_use`` blocks become one
        ``event_type='tool_use'`` row; a terminal ``result`` event
        becomes one ``event_type='result'`` row carrying token counts.
        Returns the number of events recorded.
        """
        count = 0
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(ev, dict):
                continue
            ev_type = ev.get("type")
            if ev_type == "assistant":
                msg = ev.get("message")
                if not isinstance(msg, dict):
                    continue
                for block in msg.get("content", []) or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") != "tool_use":
                        continue
                    self.record_event(
                        config=config,
                        task_id=task_id,
                        event_type="tool_use",
                        tool_name=str(block.get("name") or ""),
                        tool_input=_safe_json(block.get("input")),
                        tool_output=None,
                    )
                    count += 1
            elif ev_type == "result":
                usage = ev.get("usage") or {}
                self.record_event(
                    config=config,
                    task_id=task_id,
                    event_type="result",
                    tool_name=None,
                    tool_input=None,
                    tool_output=_safe_json(ev.get("result")),
                    input_tokens=_as_int(usage.get("input_tokens")),
                    output_tokens=_as_int(usage.get("output_tokens")),
                )
                count += 1
        return count

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _row_bytes(
        *,
        event_type: str,
        tool_name: str | None,
        tool_input: str | None,
        tool_output: str | None,
    ) -> int:
        return (
            len(event_type)
            + len(tool_name or "")
            + len(tool_input or "")
            + len(tool_output or "")
            + _ROW_OVERHEAD_BYTES
        )

    def _handle_overflow(
        self,
        *,
        config: str,
        task_id: str,
        incoming: int,
        task_overflow: bool,
        run_overflow: bool,
        current_task: int,
    ) -> None:
        """Raise or truncate. Caller holds ``self._lock``."""
        # Run-scope overflow ALWAYS fails loudly — the PRD only allows
        # truncate at task scope.
        if run_overflow:
            raise TraceBudgetExceeded(
                scope="run",
                config=config,
                task_id=task_id,
                limit=self._run_budget,
                current=self._run_bytes,
                incoming=incoming,
            )

        # Task-scope overflow: respect overflow policy.
        if self._overflow is TraceOverflowPolicy.FAIL:
            raise TraceBudgetExceeded(
                scope="task",
                config=config,
                task_id=task_id,
                limit=self._task_budget,
                current=current_task,
                incoming=incoming,
            )

        # TRUNCATE: write a marker row (NOT budget-checked — marker is
        # tiny and the point of truncate is to stop further writes).
        key = (config, task_id)
        seq = self._seq.get(key, 0)
        self._seq[key] = seq + 1
        marker_bytes = self._row_bytes(
            event_type="trace_truncated",
            tool_name=None,
            tool_input=None,
            tool_output=None,
        )
        self._pending.append(
            _PendingRow(
                run_id=self._run_id,
                config=config,
                task_id=task_id,
                event_seq=seq,
                ts=time.time(),
                event_type="trace_truncated",
                tool_name=None,
                tool_input=None,
                tool_output=None,
                duration_ms=None,
                input_tokens=None,
                output_tokens=None,
                bytes_written=marker_bytes,
            )
        )
        # Flush immediately so the marker row is durable even if the
        # process dies before the next batch.
        self._flush_locked()
        self._truncated.add(key)
        logger.warning(
            "Trace TRUNCATE policy triggered: config=%s task_id=%s "
            "task_bytes=%d limit=%d — dropping further events for this task",
            config,
            task_id,
            current_task,
            self._task_budget,
        )

    def _flush_locked(self) -> None:
        """Commit pending rows. Caller holds ``self._lock``.

        Uses BEGIN IMMEDIATE so concurrent writers from other threads
        or processes serialize at the DB level. PRIMARY KEY on
        ``(run_id, config, task_id, event_seq)`` means INSERT is
        idempotent on retry.
        """
        if not self._pending or self._conn is None:
            return
        rows = self._pending
        self._pending = []

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.executemany(
                "INSERT OR IGNORE INTO events "
                "(run_id, config, task_id, event_seq, ts, event_type, "
                " tool_name, tool_input, tool_output, duration_ms, "
                " input_tokens, output_tokens, bytes_written) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        r.run_id,
                        r.config,
                        r.task_id,
                        r.event_seq,
                        r.ts,
                        r.event_type,
                        r.tool_name,
                        r.tool_input,
                        r.tool_output,
                        r.duration_ms,
                        r.input_tokens,
                        r.output_tokens,
                        r.bytes_written,
                    )
                    for r in rows
                ],
            )
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            # Re-enqueue rows so a later flush can retry. This matters
            # for transient lock contention — genuine corruption will
            # surface on the retry.
            self._pending = rows + self._pending
            raise


def _safe_json(value: Any) -> str | None:
    """Serialize *value* to JSON for storage; None passes through."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def iter_pending(recorder: TraceRecorder) -> Iterable[_PendingRow]:
    """Expose the pending buffer for testing (read-only view)."""
    return tuple(recorder._pending)
