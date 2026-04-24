"""Tenant-scoped file lock — serializes concurrent mine / run / snapshot invocations.

Implements PRD R4 (codeprobe v0.7): two codeprobe invocations running in the
same cwd under the same ``$USER`` share a tenant (via ``derive_tenant``) and
therefore a state directory under ``~/.codeprobe/state/{tenant}/``. When they
also target the same command (mine / run / snapshot), they can race on state
writes and corrupt each other. The existing ``{slug}@{user}`` tenant shape
plus the CI fail-loud guard mitigates the cross-tenant case, but the intra-
tenant same-command race is still possible locally.

This module adds an explicit advisory file lock scoped to
``(tenant_id, command)``. The lockfile lives at
``~/.codeprobe/state/{tenant}/.lock-{command}`` and holds the PID of the
current holder. On contention the contending caller raises a
``DiagnosticError`` with code ``TENANT_IN_USE`` naming the live PID.

Stale locks (the holder PID is no longer running) are reclaimed
transparently: ``fcntl.flock`` itself is per-file-handle and released when
the holder process dies, and we additionally rewrite the PID after acquire
so readers always see a live PID.

Windows is out of scope today — codeprobe does not claim Windows support.
Falling back to a degraded mode on Windows is intentional (no lock; same
behaviour as pre-0.7) so codeprobe at least functions in development there.
"""

from __future__ import annotations

import errno
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Literal

from codeprobe.paths import tenant_root

try:  # pragma: no cover - platform shim
    import fcntl  # type: ignore[import-not-found, unused-ignore]

    _FCNTL_AVAILABLE = True
except ImportError:  # pragma: no cover - Windows path
    fcntl = None  # type: ignore[assignment]
    _FCNTL_AVAILABLE = False

logger = logging.getLogger(__name__)

TenantCommand = Literal["mine", "run", "snapshot"]

_LOCK_ENV_DISABLE = "CODEPROBE_DISABLE_TENANT_LOCK"


def _pid_alive(pid: int) -> bool:
    """Return True when a process with *pid* exists on this host.

    ``os.kill(pid, 0)`` is the POSIX-portable "is this PID alive" probe: it
    performs the usual permission check but does not actually send a signal.
    Signal 0 is guaranteed to never be delivered. Raises ``OSError(ESRCH)``
    when the PID is free.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A PID owned by another user is still alive — treat as held.
        return True
    except OSError as exc:  # pragma: no cover - defensive
        return exc.errno != errno.ESRCH
    return True


def _read_lock_pid(lock_path: Path) -> int | None:
    """Return the PID recorded in *lock_path*, or None when unreadable."""
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _raise_in_use(tenant_id: str, command: str, holder_pid: int | None) -> None:
    """Raise :class:`DiagnosticError` ``TENANT_IN_USE`` with actionable context.

    Imported locally to avoid circular imports between ``cli.errors`` and
    any module that wants to acquire a lock.
    """
    from codeprobe.cli.errors import DiagnosticError

    holder_desc = f"pid={holder_pid}" if holder_pid else "unknown"
    raise DiagnosticError(
        code="TENANT_IN_USE",
        message=(
            f"codeprobe {command} is already running for tenant {tenant_id!r} "
            f"({holder_desc}). Only one {command} may run per tenant at a time."
        ),
        diagnose_cmd=(
            f"ps -p {holder_pid}" if holder_pid else "codeprobe doctor"
        ),
        terminal=True,
        next_steps=[
            (
                "Wait for the running invocation to finish, then retry",
                f"codeprobe {command}",
            ),
            (
                "If you believe the holder is dead, remove the lockfile",
                (
                    "rm ~/.codeprobe/state/"
                    f"{tenant_id}/.lock-{command}"
                ),
            ),
        ],
        detail={
            "tenant": tenant_id,
            "command": command,
            "holder_pid": holder_pid,
        },
    )


def _lock_path(tenant_id: str, command: TenantCommand) -> Path:
    """Return the lockfile path for ``(tenant_id, command)``."""
    return tenant_root(tenant_id) / f".lock-{command}"


@contextmanager
def acquire_tenant_lock(
    tenant_id: str,
    command: TenantCommand,
) -> Iterator[Path]:
    """Acquire an exclusive advisory lock on ``(tenant_id, command)``.

    Yields the lockfile path so callers can log it. On success the
    lockfile contains the holder PID. On contention (another live PID
    holds the lock) a :class:`DiagnosticError` ``TENANT_IN_USE`` is
    raised immediately — non-blocking by design so agent wrappers fail
    fast with a clear message instead of hanging.

    Release is automatic on context exit; the lockfile is unlinked
    (best-effort) so the next caller starts from a clean state instead
    of inheriting a stale PID.

    On platforms without ``fcntl`` (Windows), the lock is a no-op and a
    warning is logged. This preserves the pre-R4 behaviour on
    unsupported platforms rather than hard-failing there.

    Setting ``CODEPROBE_DISABLE_TENANT_LOCK=1`` bypasses the lock
    entirely — reserved for tests of the lock itself and emergency
    operator overrides.
    """
    if os.environ.get(_LOCK_ENV_DISABLE) == "1":
        yield _lock_path(tenant_id, command)
        return

    if not _FCNTL_AVAILABLE:
        logger.warning(
            "Tenant lock is a no-op on this platform (no fcntl). "
            "Concurrent %s invocations may race.",
            command,
        )
        yield _lock_path(tenant_id, command)
        return

    root = tenant_root(tenant_id)
    root.mkdir(parents=True, exist_ok=True)
    lock_path = _lock_path(tenant_id, command)

    # Open for read+write, create if missing. We open before trying to
    # flock so the kernel has a handle to key the lock on. O_CLOEXEC is
    # inherited by default on Python 3.4+.
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fh: IO[str] | None = None
    acquired = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder_pid = _read_lock_pid(lock_path)
            if holder_pid is not None and not _pid_alive(holder_pid):
                # Stale PID recorded but the kernel still has the lock
                # held by some other descriptor. This is rare — trust
                # fcntl and surface TENANT_IN_USE with holder_pid=None
                # so the operator can investigate.
                _raise_in_use(tenant_id, command, holder_pid=None)
            _raise_in_use(tenant_id, command, holder_pid=holder_pid)
        except OSError as exc:  # pragma: no cover - defensive
            _raise_in_use(tenant_id, command, holder_pid=_read_lock_pid(lock_path))
            raise exc

        acquired = True
        # Re-open as a regular file handle so we can use text IO.
        fh = os.fdopen(fd, "r+", encoding="utf-8")
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()))
        fh.flush()

        try:
            yield lock_path
        finally:
            # Best-effort cleanup: truncate the PID record and unlink
            # the lockfile so the next caller sees a clean slate.
            try:
                fh.seek(0)
                fh.truncate()
                fh.flush()
            except Exception:  # pragma: no cover - best-effort
                logger.debug("Failed to truncate lockfile %s on release", lock_path)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:  # pragma: no cover - best-effort
                logger.debug("Failed to unlink lockfile %s on release", lock_path)
    finally:
        if fh is not None:
            try:
                if acquired:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:  # pragma: no cover - best-effort
                pass
            fh.close()
        else:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover
                pass


__all__ = ["acquire_tenant_lock", "TenantCommand"]
