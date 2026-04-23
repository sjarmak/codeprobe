"""Tests for codeprobe.trace.store — schema, indexes, migration."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from codeprobe.trace.store import SCHEMA_VERSION, open_store


def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
        (table,),
    ).fetchall()
    # Drop sqlite_autoindex_* (auto-created for PRIMARY KEY).
    return {r[0] for r in rows if not r[0].startswith("sqlite_")}


@pytest.mark.unit
def test_open_creates_schema(tmp_path: Path) -> None:
    db = tmp_path / "trace.db"
    conn = open_store(db)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "events" in tables
        assert "schema_migrations" in tables

        versions = [
            row[0] for row in conn.execute("SELECT version FROM schema_migrations")
        ]
        assert SCHEMA_VERSION in versions
    finally:
        conn.close()


@pytest.mark.unit
def test_events_has_required_columns(tmp_path: Path) -> None:
    db = tmp_path / "trace.db"
    conn = open_store(db)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    finally:
        conn.close()
    expected = {
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
    }
    assert expected <= cols


@pytest.mark.unit
def test_indexes_exist(tmp_path: Path) -> None:
    """Acceptance criterion (3): named indexes on (config, task_id), tool_name, ts."""
    db = tmp_path / "trace.db"
    conn = open_store(db)
    try:
        names = _index_names(conn, "events")
    finally:
        conn.close()
    assert "idx_events_config_task" in names
    assert "idx_events_tool_name" in names
    assert "idx_events_ts" in names


@pytest.mark.unit
def test_reopen_preserves_rows(tmp_path: Path) -> None:
    db = tmp_path / "trace.db"
    conn = open_store(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for i in range(5):
            conn.execute(
                "INSERT INTO events "
                "(run_id, config, task_id, event_seq, ts, event_type, bytes_written) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("r", "c", "t", i, time.time(), "tool_use", 100),
            )
        conn.execute("COMMIT")
    finally:
        conn.close()

    conn2 = open_store(db)
    try:
        count = conn2.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn2.close()
    assert count == 5


@pytest.mark.unit
def test_migrate_idempotent(tmp_path: Path) -> None:
    """Opening the same DB twice must not duplicate migration rows."""
    db = tmp_path / "trace.db"
    open_store(db).close()
    open_store(db).close()

    conn = sqlite3.connect(str(db))
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
            (SCHEMA_VERSION,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


@pytest.mark.unit
def test_primary_key_enforces_uniqueness(tmp_path: Path) -> None:
    db = tmp_path / "trace.db"
    conn = open_store(db)
    try:
        conn.execute(
            "INSERT INTO events "
            "(run_id, config, task_id, event_seq, ts, event_type, bytes_written) "
            "VALUES ('r','c','t',0,1.0,'tool_use',10)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO events "
                "(run_id, config, task_id, event_seq, ts, event_type, bytes_written) "
                "VALUES ('r','c','t',0,2.0,'tool_use',10)"
            )
    finally:
        conn.close()
