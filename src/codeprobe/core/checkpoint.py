"""SQLite-backed checkpoint store for experiment progress tracking.

Replaces the legacy JSONL checkpoint with a durable, concurrent-safe
SQLite database using WAL mode.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from codeprobe.models.experiment import CompletedTask

logger = logging.getLogger(__name__)

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS checkpoints (
    task_id       TEXT NOT NULL,
    config_name   TEXT NOT NULL,
    automated_score REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'completed',
    completed_at  TEXT NOT NULL,
    metadata      TEXT NOT NULL DEFAULT '{}',
    result_json   TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (task_id, config_name)
)
"""

_MIGRATE_STATUS_COLUMN = (
    "ALTER TABLE checkpoints ADD COLUMN status TEXT NOT NULL DEFAULT 'completed'"
)
_MIGRATE_RESULT_JSON_COLUMN = (
    "ALTER TABLE checkpoints ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}'"
)


class CheckpointStore:
    """Persistent checkpoint store backed by SQLite with WAL mode.

    Each store instance is scoped to a single *config_name* so that
    multiple experiment configurations can share the same database file.
    """

    def __init__(self, db_path: Path, *, config_name: str) -> None:
        self._db_path = db_path
        self._config_name = config_name
        self._conn = self._open(db_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def append(self, task: CompletedTask) -> None:
        """Insert or update a checkpoint entry for the given task."""
        now = datetime.now(timezone.utc).isoformat()
        metadata_json = json.dumps(task.metadata) if task.metadata else "{}"
        result_json = json.dumps(asdict(task))
        self._conn.execute(
            "INSERT INTO checkpoints "
            "(task_id, config_name, automated_score, status, completed_at, metadata, result_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (task_id, config_name) DO UPDATE SET "
            "  automated_score = excluded.automated_score, "
            "  status = excluded.status, "
            "  completed_at = excluded.completed_at, "
            "  metadata = excluded.metadata, "
            "  result_json = excluded.result_json",
            (
                task.task_id,
                self._config_name,
                task.automated_score,
                task.status,
                now,
                metadata_json,
                result_json,
            ),
        )
        self._conn.commit()

    def load_ids(self) -> set[str]:
        """Return the set of successfully completed task IDs for this config.

        Tasks with status ``'error'`` are excluded so they get retried.
        """
        rows = self._conn.execute(
            "SELECT task_id FROM checkpoints "
            "WHERE config_name = ? AND status != 'error'",
            (self._config_name,),
        ).fetchall()
        return {row[0] for row in rows}

    def load_entries(self) -> list[dict]:
        """Return checkpoint entries for this config as dicts.

        Only returns successfully completed entries (status != 'error')
        so that failed tasks are retried on the next run.
        Ordered by insertion (rowid).

        When ``result_json`` is present (new schema), returns the full
        CompletedTask fields.  Falls back to minimal fields for old rows.
        """
        rows = self._conn.execute(
            "SELECT task_id, automated_score, completed_at, metadata, status, result_json "
            "FROM checkpoints WHERE config_name = ? AND status != 'error' "
            "ORDER BY rowid",
            (self._config_name,),
        ).fetchall()
        entries: list[dict] = []
        for row in rows:
            result_json = row[5] if len(row) > 5 else "{}"
            result_data = json.loads(result_json) if result_json else {}
            if result_data and "task_id" in result_data:
                # Full CompletedTask stored — use it directly
                entries.append(result_data)
            else:
                # Legacy row — minimal fields only
                entries.append(
                    {
                        "task_id": row[0],
                        "automated_score": row[1],
                        "completed_at": row[2],
                        "metadata": json.loads(row[3]) if row[3] else {},
                        "status": row[4],
                    }
                )
        return entries

    def close(self) -> None:
        """Close the underlying database connection."""
        self._conn.close()

    # ------------------------------------------------------------------
    # Factory: legacy JSONL migration
    # ------------------------------------------------------------------

    @classmethod
    def from_legacy_path(
        cls,
        jsonl_path: Path,
        db_path: Path,
        *,
        config_name: str,
    ) -> CheckpointStore:
        """Create a store, auto-migrating from JSONL if the DB doesn't exist.

        If *db_path* already exists, the JSONL file is ignored.
        If *db_path* does not exist but *jsonl_path* does, entries are
        migrated into a new database.
        """
        needs_migration = not db_path.exists() and jsonl_path.is_file()
        store = cls(db_path, config_name=config_name)

        if needs_migration:
            store._migrate_jsonl(jsonl_path)

        return store

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _open(self, db_path: Path) -> sqlite3.Connection:
        """Open (or create) the SQLite database with WAL mode.

        If the file is corrupt, it is removed and recreated.
        Automatically migrates the schema for older databases.
        """
        db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            conn = sqlite3.connect(str(db_path), timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE_TABLE)
            self._migrate_schema(conn)
            conn.commit()
            return conn
        except sqlite3.DatabaseError:
            logger.warning(
                "Corrupt checkpoint DB at %s — removing and recreating", db_path
            )
            db_path.unlink(missing_ok=True)
            # Also remove WAL/SHM sidecar files
            db_path.with_suffix(".db-wal").unlink(missing_ok=True)
            db_path.with_suffix(".db-shm").unlink(missing_ok=True)
            conn = sqlite3.connect(str(db_path), timeout=10)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_CREATE_TABLE)
            self._migrate_schema(conn)
            conn.commit()
            return conn

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        """Add columns introduced after the initial schema."""
        cols = {
            row[1] for row in conn.execute("PRAGMA table_info(checkpoints)").fetchall()
        }
        if "status" not in cols:
            conn.execute(_MIGRATE_STATUS_COLUMN)
            logger.info("Migrated checkpoint schema: added 'status' column")
        if "result_json" not in cols:
            conn.execute(_MIGRATE_RESULT_JSON_COLUMN)
            logger.info("Migrated checkpoint schema: added 'result_json' column")

    def _migrate_jsonl(self, jsonl_path: Path) -> None:
        """Import entries from a legacy JSONL checkpoint file."""
        logger.info("Migrating JSONL checkpoint %s to SQLite", jsonl_path)
        count = 0
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "Skipping malformed JSONL line during migration: %s",
                        line[:80],
                    )
                    continue
                if "task_id" not in entry:
                    continue
                task = CompletedTask(
                    task_id=entry["task_id"],
                    automated_score=entry.get("automated_score", 0.0),
                )
                self.append(task)
                count += 1
        logger.info("Migrated %d entries from JSONL to SQLite", count)
