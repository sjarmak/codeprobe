"""Resumable mining state persisted to a tenant-scoped SQLite database.

Design goals (R15 / INV2):

- Per-commit status is durable across process death and host reboots.
  WAL + ``synchronous=FULL`` is the minimal pair that survives
  SIGKILL-mid-write on POSIX without corruption.
- State is scoped per tenant + per repo hash, so parallel mines against
  distinct repos never share a database file.
- Resume is *allowlist-based*: the resume query selects commits with
  ``status='completed'``. A row in any other state (including a crashed
  ``running``) is re-processed on the next run. The startup sweep
  flips leftover ``running``/``pending`` rows to ``interrupted`` so
  callers can distinguish an in-flight crash from a never-started SHA.
- Worktree create/remove critical sections are serialized with a
  repo-level ``fcntl.flock`` on ``<repo>/.git/codeprobe.lock``.

The module intentionally keeps the public surface small: open, the five
``record_*`` methods, ``completed_shas()``, ``startup_sweep()``, and
``worktree_lock()``. Anything more is orchestration, not state.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import random
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

from codeprobe.paths import DEFAULT_TENANT, tenant_state_dir

logger = logging.getLogger(__name__)

_DB_FILENAME = "mine.db"
_BUSY_TIMEOUT_MS = 30_000
# Backoff bounds for the WAL conversion retry in :func:`_enable_wal`.
_WAL_RETRY_INITIAL_DELAY_S = 0.005
_WAL_RETRY_MAX_DELAY_S = 0.1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS commits (
    sha          TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'pending',
    started_at   REAL,
    completed_at REAL,
    error        TEXT
)
"""

VALID_STATUSES = frozenset({"pending", "running", "completed", "interrupted", "failed"})


class MineState:
    """SQLite-backed mining state.

    Typical usage::

        with MineState.open(tenant_id="acme", repo_hash=h) as state:
            for sha in candidate_shas:
                if sha in state.completed_shas():
                    continue
                state.record_running(sha)
                try:
                    process(sha)
                except Exception as exc:
                    state.record_interrupted(sha, error=str(exc))
                    raise
                state.record_completed(sha)
    """

    def __init__(self, conn: sqlite3.Connection, db_path: Path) -> None:
        self._conn = conn
        self._db_path = db_path

    # ------------------------------------------------------------------
    # construction / teardown
    # ------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        *,
        tenant_id: str = DEFAULT_TENANT,
        repo_hash: str,
        sweep: bool = True,
    ) -> MineState:
        """Open (or create) the mine.db for a (tenant_id, repo_hash) pair.

        When *sweep* is True (default) a startup sweep promotes any
        leftover ``running``/``pending`` rows to ``interrupted`` before
        returning. Callers that want to inspect raw state (e.g. tests)
        can pass ``sweep=False``.
        """
        state_dir = tenant_state_dir(tenant_id=tenant_id, repo_hash=repo_hash)
        # tenant_state_dir is pure — it doesn't materialize the directory.
        # MineState owns the lifecycle of the mine.db file, so it also
        # owns the directory creation (0o700 for tenant privacy).
        state_dir.mkdir(parents=True, exist_ok=True)
        try:
            import os as _os

            _os.chmod(state_dir, 0o700)
        except OSError:
            # Best-effort on filesystems that don't support chmod.
            pass
        db_path = state_dir / _DB_FILENAME

        # ``isolation_level=None`` puts sqlite3 into autocommit mode; we
        # drive transactions explicitly via ``BEGIN IMMEDIATE`` so two
        # writers don't deadlock fighting for the RESERVED lock.
        conn = sqlite3.connect(
            str(db_path),
            isolation_level=None,
            timeout=_BUSY_TIMEOUT_MS / 1000.0,
            check_same_thread=False,
        )
        try:
            _apply_pragmas(conn)
            conn.execute(_SCHEMA)
        except sqlite3.Error:
            # No MineState will own this connection, so nothing will ever
            # close it. Notably reachable: _enable_wal raising on a database
            # a sibling worker is holding, which a caller may well retry.
            conn.close()
            raise

        state = cls(conn, db_path)
        if sweep:
            state.startup_sweep()
        return state

    @property
    def db_path(self) -> Path:
        """Path to the underlying SQLite database file (for diagnostics)."""
        return self._db_path

    def close(self) -> None:
        """Close the underlying connection. Idempotent."""
        try:
            self._conn.close()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            logger.debug("MineState.close error: %s", exc)

    def __enter__(self) -> MineState:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: object,
    ) -> None:
        self.close()

    # ------------------------------------------------------------------
    # writers (all wrapped in BEGIN IMMEDIATE)
    # ------------------------------------------------------------------

    def record_pending(self, sha: str) -> None:
        """Insert a sha as pending, or no-op if already present."""
        _check_sha(sha)
        with self._write():
            self._conn.execute(
                "INSERT OR IGNORE INTO commits (sha, status) VALUES (?, 'pending')",
                (sha,),
            )

    def record_running(self, sha: str) -> None:
        """Mark *sha* as running and stamp started_at.

        Upserts so a caller may transition ``pending``/``interrupted`` ->
        ``running`` on retry without an explicit existence check.
        """
        _check_sha(sha)
        now = time.time()
        with self._write():
            self._conn.execute(
                """
                INSERT INTO commits (sha, status, started_at)
                VALUES (?, 'running', ?)
                ON CONFLICT(sha) DO UPDATE SET
                    status='running',
                    started_at=excluded.started_at,
                    error=NULL
                """,
                (sha, now),
            )

    def record_completed(self, sha: str) -> None:
        """Mark *sha* completed and stamp completed_at."""
        _check_sha(sha)
        now = time.time()
        with self._write():
            self._conn.execute(
                """
                INSERT INTO commits (sha, status, completed_at)
                VALUES (?, 'completed', ?)
                ON CONFLICT(sha) DO UPDATE SET
                    status='completed',
                    completed_at=excluded.completed_at,
                    error=NULL
                """,
                (sha, now),
            )

    def record_interrupted(self, sha: str, *, error: str = "") -> None:
        """Mark *sha* interrupted with an optional error message."""
        _check_sha(sha)
        with self._write():
            self._conn.execute(
                """
                INSERT INTO commits (sha, status, error)
                VALUES (?, 'interrupted', ?)
                ON CONFLICT(sha) DO UPDATE SET
                    status='interrupted',
                    error=excluded.error
                """,
                (sha, error or None),
            )

    def record_failed(self, sha: str, *, error: str = "") -> None:
        """Mark *sha* as failed (retries exhausted, permanent failure)."""
        _check_sha(sha)
        with self._write():
            self._conn.execute(
                """
                INSERT INTO commits (sha, status, error)
                VALUES (?, 'failed', ?)
                ON CONFLICT(sha) DO UPDATE SET
                    status='failed',
                    error=excluded.error
                """,
                (sha, error or None),
            )

    def startup_sweep(self) -> int:
        """Promote leftover ``running``/``pending`` rows to ``interrupted``.

        Returns the number of rows affected. Safe to call multiple times.
        """
        with self._write():
            cur = self._conn.execute(
                "UPDATE commits SET status='interrupted' "
                "WHERE status IN ('running','pending')"
            )
            affected = cur.rowcount if cur.rowcount is not None else 0
        if affected:
            logger.info(
                "MineState startup sweep: %d in-flight row(s) -> interrupted "
                "(db=%s)",
                affected,
                self._db_path,
            )
        return affected

    # ------------------------------------------------------------------
    # readers
    # ------------------------------------------------------------------

    def completed_shas(self) -> set[str]:
        """Return the set of commit SHAs with status='completed'.

        Allowlist query (per INV2): any other status — including a
        still-running row written in this process — is excluded so a
        crash mid-mine never leaves a commit wrongly "skipped".
        """
        cur = self._conn.execute(
            "SELECT sha FROM commits WHERE status='completed'"
        )
        return {row[0] for row in cur.fetchall()}

    def status(self, sha: str) -> str | None:
        """Return the recorded status of *sha*, or None when unknown."""
        _check_sha(sha)
        cur = self._conn.execute(
            "SELECT status FROM commits WHERE sha=?", (sha,)
        )
        row = cur.fetchone()
        return row[0] if row else None

    def all_rows(self) -> list[tuple[str, str, float | None, float | None, str | None]]:
        """Return every row (for diagnostics and tests)."""
        cur = self._conn.execute(
            "SELECT sha, status, started_at, completed_at, error FROM commits"
        )
        return list(cur.fetchall())

    # ------------------------------------------------------------------
    # worktree lock
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def worktree_lock(self, git_dir: Path) -> Iterator[None]:
        """Serialize worktree create/remove via ``fcntl.flock`` on *git_dir*.

        Uses ``<git_dir>/codeprobe.lock`` as a dedicated lock file so we
        never contend with git's own index lock. Parallel workers
        holding this lock exclusively ensures ``git worktree add`` /
        ``git worktree remove`` see a consistent repository state.
        """
        git_dir = Path(git_dir)
        git_dir.mkdir(parents=True, exist_ok=True)
        lock_path = git_dir / "codeprobe.lock"
        with open(lock_path, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError as exc:  # pragma: no cover - defensive
                    logger.debug("flock unlock failed: %s", exc)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _write(self) -> Iterator[None]:
        """Run a write inside ``BEGIN IMMEDIATE ... COMMIT``.

        ``BEGIN IMMEDIATE`` acquires the RESERVED lock up front so two
        concurrent writers serialize deterministically rather than both
        entering the transaction and one later failing with
        ``SQLITE_BUSY`` at commit.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")


# ----------------------------------------------------------------------
# module-private helpers
# ----------------------------------------------------------------------


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Apply busy_timeout + WAL + synchronous=FULL to *conn*.

    These settings are required by INV2. They are executed outside any
    transaction — ``journal_mode`` can only change when the database
    has no open write transactions.

    ``busy_timeout`` goes first so the busy handler is installed before
    anything can contend, and the WAL conversion runs through
    :func:`_enable_wal` rather than a bare ``PRAGMA`` — see that function
    for why.
    """
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    _enable_wal(conn)
    conn.execute("PRAGMA synchronous=FULL")


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Put *conn*'s database into WAL mode, waiting out concurrent writers.

    Converting a database off its default rollback journal rewrites the file
    header, which needs a brief exclusive lock. SQLite does not route that
    contention through the busy handler: while another connection holds the
    database, the pragma fails with ``SQLITE_BUSY`` *immediately*, ignoring
    ``busy_timeout`` entirely. Parallel mine workers raced on it — the first
    worker to reach its ``BEGIN IMMEDIATE`` would knock over any sibling still
    doing the one-time conversion, surfacing as an intermittent ``database is
    locked`` out of ``MineState.open`` (codeprobe-uhr4).

    The wait is therefore ours to do: retry until the pragma reports ``wal``,
    bounded by the same ``busy_timeout`` budget that governs every other lock
    wait in this module, and raise on exhaustion. Once the database is in WAL
    the pragma is a no-op, so every open after the first returns on the first
    attempt without sleeping.

    Raises:
        sqlite3.OperationalError: the database could not be moved into WAL
            mode within the busy-timeout budget.
    """
    budget_s = _BUSY_TIMEOUT_MS / 1000.0
    started = time.monotonic()
    delay = _WAL_RETRY_INITIAL_DELAY_S
    attempts = 0
    # The SQLITE_BUSY behind the last failed attempt, carried out of the
    # ``except`` block so the give-up raise can chain it. The quiet-no-op
    # branch below clears it: that failure has no exception to name.
    last_exc: sqlite3.OperationalError | None = None
    while True:
        attempts += 1
        try:
            row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
        except sqlite3.OperationalError as exc:
            last_exc = exc
            reason = str(exc)
        else:
            # A blocked conversion can also come back as a quiet no-op that
            # reports the *old* mode instead of raising. Treat that as a
            # failure too: running on a rollback journal silently would break
            # the durability pair INV2 asks for.
            mode = str(row[0]).lower() if row else "unknown"
            if mode == "wal":
                return
            last_exc = None
            reason = f"journal_mode is {mode!r}, expected 'wal'"

        elapsed = time.monotonic() - started
        extra = {"wal_attempts": attempts, "wal_elapsed_s": elapsed}
        if elapsed >= budget_s:
            logger.warning(
                "gave up enabling WAL after %d attempt(s) in %.3fs: %s",
                attempts,
                elapsed,
                reason,
                extra=extra,
            )
            raise sqlite3.OperationalError(
                f"could not enable WAL journal mode within "
                f"{_BUSY_TIMEOUT_MS}ms: {reason}"
            ) from last_exc
        logger.debug(
            "WAL conversion blocked (attempt %d, %.3fs elapsed), retrying: %s",
            attempts,
            elapsed,
            reason,
            extra=extra,
        )
        # Decorrelated jitter. Workers are spawned together and collide on the
        # same conversion, so an undithered doubling would march the whole pool
        # through the same retry ticks and re-collide at each one. Sleeping a
        # random point in [0, delay) spreads them instead.
        time.sleep(random.uniform(0.0, delay))
        delay = min(delay * 2, _WAL_RETRY_MAX_DELAY_S)


def _check_sha(sha: str) -> None:
    """Validate that *sha* is a non-empty hex-ish string."""
    if not isinstance(sha, str) or not sha:
        raise ValueError("sha must be a non-empty string")
