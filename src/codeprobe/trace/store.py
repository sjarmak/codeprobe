"""SQLite schema + open/close for the trace store.

The trace store persists agent lifecycle events under
``<experiment>/runs/trace.db``. Schema is versioned via the
``schema_migrations`` table so future migrations can be applied
idempotently. All writes use WAL mode for durable crash recovery —
already-committed rows survive SIGKILL mid-write.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_CREATE_EVENTS = """\
CREATE TABLE IF NOT EXISTS events (
    run_id        TEXT    NOT NULL,
    config        TEXT    NOT NULL,
    task_id       TEXT    NOT NULL,
    event_seq     INTEGER NOT NULL,
    ts            REAL    NOT NULL,
    event_type    TEXT    NOT NULL,
    tool_name     TEXT,
    tool_input    TEXT,
    tool_output   TEXT,
    duration_ms   INTEGER,
    input_tokens  INTEGER,
    output_tokens INTEGER,
    bytes_written INTEGER NOT NULL,
    PRIMARY KEY (run_id, config, task_id, event_seq)
)
"""

_CREATE_MIGRATIONS = """\
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at REAL    NOT NULL
)
"""

# Indexes satisfy acceptance criterion (3). Names chosen to match
# `.indexes events` output lexically.
_CREATE_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_events_config_task ON events (config, task_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_tool_name   ON events (tool_name)",
    "CREATE INDEX IF NOT EXISTS idx_events_ts          ON events (ts)",
)

# Ordered, numbered list of migration callables. Index i applies when
# current schema version < i+1. For v1 the initial schema IS the migration.
_MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, "initial schema"),
)


def open_store(db_path: Path) -> sqlite3.Connection:
    """Open (or create) the trace database at *db_path*.

    Uses WAL mode so committed rows survive a SIGKILL mid-write and
    multiple writer threads can proceed serially without blocking each
    other's reads. Creates tables + indexes + records the schema
    version on first open.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=10,
        isolation_level=None,  # manage transactions manually
        check_same_thread=False,
    )
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # WAL + NORMAL = durable + fast
        conn.execute(_CREATE_EVENTS)
        conn.execute(_CREATE_MIGRATIONS)
        for stmt in _CREATE_INDEXES:
            conn.execute(stmt)
        _apply_migrations(conn)
    except Exception:
        conn.close()
        raise
    return conn


def _apply_migrations(conn: sqlite3.Connection) -> None:
    """Stamp missing migration versions.

    Each version below :data:`SCHEMA_VERSION` that isn't present in
    ``schema_migrations`` gets recorded with ``applied_at=now``. The
    initial table creation lives in :data:`_CREATE_EVENTS`; this routine
    exists so future versions can run real ``ALTER TABLE`` statements
    before recording the stamp.
    """
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    applied = {row[0] for row in rows}
    now = time.time()
    for version, _label in _MIGRATIONS:
        if version in applied:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (version, now),
        )
    # No explicit COMMIT needed — isolation_level=None + DDL auto-commit.


def export_jsonl(db_path: Path, out: TextIO) -> int:
    """Export all ``events`` rows from *db_path* as JSONL to *out*.

    Returns the number of rows written. One row per line. Event ordering
    is ``(run_id, config, task_id, event_seq)`` — stable across exports
    of the same DB.
    """
    conn = sqlite3.connect(str(db_path), timeout=10)
    try:
        rows_written = 0
        for row in _iter_rows(conn):
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
            rows_written += 1
        return rows_written
    finally:
        conn.close()


def _iter_rows(conn: sqlite3.Connection) -> Iterator[dict]:
    """Yield each event as a dict in deterministic order."""
    cur = conn.execute(
        "SELECT run_id, config, task_id, event_seq, ts, event_type, "
        "       tool_name, tool_input, tool_output, duration_ms, "
        "       input_tokens, output_tokens, bytes_written "
        "FROM events "
        "ORDER BY run_id, config, task_id, event_seq"
    )
    cols = (
        "run_id",
        "config",
        "task_id",
        "event_seq",
        "ts",
        "event_type",
        "tool_name",
        "tool_input",
        "tool_output",
        "duration_ms",
        "input_tokens",
        "output_tokens",
        "bytes_written",
    )
    for row in cur:
        yield dict(zip(cols, row, strict=True))
