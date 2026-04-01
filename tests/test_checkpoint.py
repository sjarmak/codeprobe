"""Tests for the SQLite checkpoint store."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from codeprobe.core.checkpoint import CheckpointStore
from codeprobe.models.experiment import CompletedTask

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(task_id: str, score: float = 1.0) -> CompletedTask:
    return CompletedTask(task_id=task_id, automated_score=score)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestCRUD:
    def test_append_and_load_ids(self, tmp_path: Path) -> None:
        db_path = tmp_path / "checkpoint.db"
        store = CheckpointStore(db_path, config_name="default")

        store.append(_make_task("t-001", 1.0))
        store.append(_make_task("t-002", 0.0))

        ids = store.load_ids()
        assert ids == {"t-001", "t-002"}

    def test_load_entries(self, tmp_path: Path) -> None:
        db_path = tmp_path / "checkpoint.db"
        store = CheckpointStore(db_path, config_name="cfg-a")

        store.append(_make_task("t-001", 1.0))
        store.append(_make_task("t-002", 0.5))

        entries = store.load_entries()
        assert len(entries) == 2
        assert entries[0]["task_id"] == "t-001"
        assert entries[0]["automated_score"] == 1.0
        assert entries[1]["task_id"] == "t-002"
        assert entries[1]["automated_score"] == 0.5

    def test_entries_include_completed_at(self, tmp_path: Path) -> None:
        db_path = tmp_path / "checkpoint.db"
        store = CheckpointStore(db_path, config_name="default")

        store.append(_make_task("t-001"))

        entries = store.load_entries()
        assert "completed_at" in entries[0]
        # Should be a valid ISO format
        datetime.fromisoformat(entries[0]["completed_at"])

    def test_entries_include_metadata(self, tmp_path: Path) -> None:
        db_path = tmp_path / "checkpoint.db"
        store = CheckpointStore(db_path, config_name="default")

        task = CompletedTask(
            task_id="t-001",
            automated_score=1.0,
            metadata={"agent": "claude"},
        )
        store.append(task)

        entries = store.load_entries()
        assert entries[0]["metadata"] == {"agent": "claude"}

    def test_upsert_on_duplicate(self, tmp_path: Path) -> None:
        """Second append for same (task_id, config_name) updates the row."""
        db_path = tmp_path / "checkpoint.db"
        store = CheckpointStore(db_path, config_name="default")

        store.append(_make_task("t-001", 0.0))
        store.append(_make_task("t-001", 1.0))

        entries = store.load_entries()
        assert len(entries) == 1
        assert entries[0]["automated_score"] == 1.0

    def test_config_isolation(self, tmp_path: Path) -> None:
        """Different config_name values see different checkpoints."""
        db_path = tmp_path / "checkpoint.db"
        store_a = CheckpointStore(db_path, config_name="cfg-a")
        store_b = CheckpointStore(db_path, config_name="cfg-b")

        store_a.append(_make_task("t-001"))
        store_b.append(_make_task("t-002"))

        assert store_a.load_ids() == {"t-001"}
        assert store_b.load_ids() == {"t-002"}

    def test_empty_db(self, tmp_path: Path) -> None:
        db_path = tmp_path / "checkpoint.db"
        store = CheckpointStore(db_path, config_name="default")

        assert store.load_ids() == set()
        assert store.load_entries() == []


# ---------------------------------------------------------------------------
# WAL mode
# ---------------------------------------------------------------------------


class TestWALMode:
    def test_wal_mode_enabled(self, tmp_path: Path) -> None:
        db_path = tmp_path / "checkpoint.db"
        CheckpointStore(db_path, config_name="default")

        conn = sqlite3.connect(str(db_path))
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        conn.close()
        assert mode == "wal"


# ---------------------------------------------------------------------------
# Concurrent writes
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_concurrent_writes(self, tmp_path: Path) -> None:
        db_path = tmp_path / "checkpoint.db"
        errors: list[Exception] = []

        def writer(config: str, start: int, count: int) -> None:
            try:
                store = CheckpointStore(db_path, config_name=config)
                for i in range(start, start + count):
                    store.append(_make_task(f"t-{i:04d}", float(i % 2)))
            except Exception as exc:
                errors.append(exc)

        threads = [
            threading.Thread(target=writer, args=("cfg-a", 0, 50)),
            threading.Thread(target=writer, args=("cfg-b", 100, 50)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent write errors: {errors}"

        store_a = CheckpointStore(db_path, config_name="cfg-a")
        store_b = CheckpointStore(db_path, config_name="cfg-b")
        assert len(store_a.load_ids()) == 50
        assert len(store_b.load_ids()) == 50


# ---------------------------------------------------------------------------
# JSONL migration
# ---------------------------------------------------------------------------


class TestJSONLMigration:
    def test_auto_migrate_from_jsonl(self, tmp_path: Path) -> None:
        """If .jsonl exists and .db does not, auto-migrate."""
        jsonl_path = tmp_path / "checkpoint.jsonl"
        db_path = tmp_path / "checkpoint.db"

        jsonl_path.write_text(
            '{"task_id": "t-001", "automated_score": 1.0}\n'
            '{"task_id": "t-002", "automated_score": 0.0}\n'
        )

        store = CheckpointStore.from_legacy_path(
            jsonl_path, db_path, config_name="default"
        )

        ids = store.load_ids()
        assert ids == {"t-001", "t-002"}

        entries = store.load_entries()
        assert entries[0]["automated_score"] == 1.0
        assert entries[1]["automated_score"] == 0.0

    def test_migrate_skips_malformed_lines(self, tmp_path: Path) -> None:
        jsonl_path = tmp_path / "checkpoint.jsonl"
        db_path = tmp_path / "checkpoint.db"

        jsonl_path.write_text(
            '{"task_id": "t-001", "automated_score": 1.0}\n'
            "not valid json\n"
            '{"task_id": "t-002", "automated_score": 0.5}\n'
        )

        store = CheckpointStore.from_legacy_path(
            jsonl_path, db_path, config_name="default"
        )
        assert store.load_ids() == {"t-001", "t-002"}

    def test_migrate_no_op_when_db_exists(self, tmp_path: Path) -> None:
        """If .db already exists, migration is skipped."""
        jsonl_path = tmp_path / "checkpoint.jsonl"
        db_path = tmp_path / "checkpoint.db"

        # Create the db first with one entry
        store = CheckpointStore(db_path, config_name="default")
        store.append(_make_task("t-existing"))

        # Write a JSONL with a *different* entry
        jsonl_path.write_text('{"task_id": "t-new", "automated_score": 1.0}\n')

        store2 = CheckpointStore.from_legacy_path(
            jsonl_path, db_path, config_name="default"
        )
        # Should see only the DB entry, not the JSONL one
        assert store2.load_ids() == {"t-existing"}

    def test_migrate_no_jsonl(self, tmp_path: Path) -> None:
        """No JSONL file -> just create fresh DB."""
        jsonl_path = tmp_path / "checkpoint.jsonl"
        db_path = tmp_path / "checkpoint.db"

        store = CheckpointStore.from_legacy_path(
            jsonl_path, db_path, config_name="default"
        )
        assert store.load_ids() == set()


# ---------------------------------------------------------------------------
# Corrupt DB recovery
# ---------------------------------------------------------------------------


class TestCorruptRecovery:
    def test_corrupt_db_recreates(self, tmp_path: Path) -> None:
        """If the DB file is corrupt, it should be removed and recreated."""
        db_path = tmp_path / "checkpoint.db"
        db_path.write_text("this is not a sqlite database")

        store = CheckpointStore(db_path, config_name="default")
        # Should work fine after recovery
        store.append(_make_task("t-001"))
        assert store.load_ids() == {"t-001"}

    def test_error_status_excluded_from_load_ids(self, tmp_path: Path) -> None:
        """Tasks with status='error' should not appear in load_ids."""
        db_path = tmp_path / "checkpoint.db"
        store = CheckpointStore(db_path, config_name="default")

        store.append(
            CompletedTask(task_id="t-ok", automated_score=1.0, status="completed")
        )
        store.append(
            CompletedTask(task_id="t-err", automated_score=0.0, status="error")
        )

        assert store.load_ids() == {"t-ok"}

    def test_error_status_excluded_from_load_entries(self, tmp_path: Path) -> None:
        """Tasks with status='error' should not appear in load_entries."""
        db_path = tmp_path / "checkpoint.db"
        store = CheckpointStore(db_path, config_name="default")

        store.append(
            CompletedTask(task_id="t-ok", automated_score=1.0, status="completed")
        )
        store.append(
            CompletedTask(task_id="t-err", automated_score=0.0, status="error")
        )

        entries = store.load_entries()
        assert len(entries) == 1
        assert entries[0]["task_id"] == "t-ok"

    def test_error_overwritten_on_retry(self, tmp_path: Path) -> None:
        """A successful retry should overwrite the error checkpoint."""
        db_path = tmp_path / "checkpoint.db"
        store = CheckpointStore(db_path, config_name="default")

        store.append(
            CompletedTask(task_id="t-001", automated_score=0.0, status="error")
        )
        assert store.load_ids() == set()

        store.append(
            CompletedTask(task_id="t-001", automated_score=1.0, status="completed")
        )
        assert store.load_ids() == {"t-001"}

    def test_schema_migration_adds_status_column(self, tmp_path: Path) -> None:
        """Opening an old DB without status column should auto-migrate."""
        import sqlite3

        db_path = tmp_path / "checkpoint.db"
        # Create a DB with the old schema (no status column)
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE checkpoints ("
            "  task_id TEXT NOT NULL,"
            "  config_name TEXT NOT NULL,"
            "  automated_score REAL NOT NULL,"
            "  completed_at TEXT NOT NULL,"
            "  metadata TEXT NOT NULL DEFAULT '{}',"
            "  PRIMARY KEY (task_id, config_name)"
            ")"
        )
        conn.execute(
            "INSERT INTO checkpoints VALUES ('t-old', 'default', 1.0, '2025-01-01', '{}')"
        )
        conn.commit()
        conn.close()

        # Opening should auto-migrate
        store = CheckpointStore(db_path, config_name="default")
        ids = store.load_ids()
        assert "t-old" in ids
