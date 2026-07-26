"""Symlink-refusing directory writes for mined task output.

Mining reuses input and output trees across runs, so a symlink left in an
output tree (by a prior run or an attacker with control of the tree) could
redirect a task or verifier artifact outside the caller-supplied base
directory (CWE-59). :class:`SafeOutputDir` closes that gap: every directory
descent, file write, and removal is bound to a descriptor opened with
``O_NOFOLLOW``, so no symlink component is ever traversed and a check/use
swap of a path cannot redirect the operation — it follows the held
descriptor, not the re-resolved path string.

This reuses the ``O_NOFOLLOW`` descriptor-chaining idiom already used on the
read side (``codeprobe.mining.oracle_curator._read_resolved_candidate``)
rather than adding path-string prefix checks, which a symlink defeats.

Only genuine symlink / wrong-type conditions become :class:`ContainmentError`.
Operational failures (permission denied, out of space, ...) propagate with
their original exception type so callers are not misled about the cause.

Why a mining-specific primitive rather than reusing
``codeprobe.snapshot.safe_io.SecureOutputDirectory`` (per the "reuse or
extend" instruction in codeprobe-2cqg): that snapshot writer and this one
solve the same CWE-59 class but have incompatible contracts. The snapshot
primitive is exclusive-create (``O_CREAT | O_EXCL``) and *refuses* to
overwrite, fixes every file at ``0o600``, pins whole-output directory/file
identities for post-write re-verification, and can create symlinks — all
correct for immutable snapshot publication. Mining output trees are the
opposite: they are reused across runs, so writers must *overwrite* task
artifacts idempotently, must mark verifier scripts executable (``0o755``),
must *remove* stale checkpoint artifacts through the held descriptor, and
must never emit symlinks. Bending ``SecureOutputDirectory`` to add
overwrite/removal/executable modes would couple snapshot semantics to
mining cleanup and widen a security-sensitive shared surface; a small,
mining-scoped primitive that borrows the same ``O_NOFOLLOW`` descriptor
discipline is the lower-risk choice.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from types import TracebackType

__all__ = ["ContainmentError", "SafeOutputDir"]

_UNSAFE_COMPONENTS = frozenset({"", ".", ".."})

# Opening a component read-only as a directory, refusing to follow a final
# symlink. ``O_NOFOLLOW`` fails with ELOOP when the component is a symlink;
# ``O_DIRECTORY`` fails with ENOTDIR when it is a regular file.
_DIR_FLAGS = (
    os.O_RDONLY
    | os.O_DIRECTORY
    | os.O_NOFOLLOW
    | getattr(os, "O_CLOEXEC", 0)
)

# Opening a leaf file for writing, refusing to follow a final symlink.
# Deliberately WITHOUT ``O_TRUNC``: truncation must happen only after the
# fstat/link-count checks below, so a hardlinked (or otherwise wrong-type)
# target is never mutated before it is validated. ``O_NONBLOCK`` keeps a
# pre-existing FIFO from blocking the open forever.
_FILE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_NOFOLLOW
    | os.O_NONBLOCK
    | getattr(os, "O_CLOEXEC", 0)
)

# errnos that mean "a symlink or wrong-type component blocked the syscall",
# as opposed to an operational failure (EACCES, ENOSPC, EROFS, ...):
#   ELOOP   — O_NOFOLLOW hit a symlink
#   ENOTDIR — O_DIRECTORY hit a non-directory component
#   ENXIO   — O_WRONLY|O_NONBLOCK on a FIFO with no reader
#   EISDIR  — tried to open a directory for writing
_CONTAINMENT_ERRNOS = frozenset(
    {errno.ELOOP, errno.ENOTDIR, errno.ENXIO, errno.EISDIR}
)


class ContainmentError(ValueError):
    """A symlink or wrong-type component would redirect an operation outside base.

    Subclasses :class:`ValueError` so callers that already treat an unsafe
    task id as a ``ValueError`` keep the same error contract. The message
    names the offending component and the base directory it was resolved
    under — never the symlink target — so no unrelated filesystem content
    leaks into logs.
    """


def _require_component(name: str, under: Path) -> None:
    """Reject names that are not a bare, non-dot filename component."""
    if name in _UNSAFE_COMPONENTS or Path(name).name != name:
        raise ContainmentError(
            f"unsafe path component {name!r} under {under}"
        )


class SafeOutputDir:
    """A directory reached without traversing a symlink, held open by fd.

    Create the task root with :meth:`create` (the caller-supplied base is the
    trusted anchor), descend into subdirectories with :meth:`child`, write
    leaf files with :meth:`write_text` / :meth:`write_bytes`, and remove
    stale artifacts with :meth:`unlink` / :meth:`remove_tree`. Every operation
    is bound to the held descriptor. Use as a context manager so descriptors
    are always released::

        with SafeOutputDir.create(base_dir, task_id) as task_dir:
            task_dir.write_text("instruction.md", body)
            tests_dir = task_dir.child("tests")
            tests_dir.write_text("test.sh", script, executable=True)
    """

    __slots__ = ("_path", "_fd")

    def __init__(self, path: Path, fd: int) -> None:
        self._path = path
        self._fd = fd

    @classmethod
    def create(cls, base_dir: Path, name: str) -> SafeOutputDir:
        """Create ``base_dir/name`` under the trusted base, refusing symlinks.

        ``base_dir`` is the caller-supplied containment boundary and is
        materialized with ``parents=True`` — following symlinks to reach it is
        the caller's intent. Only ``name`` (and anything descended into below)
        is guarded against symlink redirection.
        """
        base_dir = Path(base_dir)
        base_dir.mkdir(parents=True, exist_ok=True)
        base_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
        base_fd = os.open(base_dir, base_flags)
        try:
            child_fd = _mkdir_open_nofollow(base_fd, name, base_dir)
        finally:
            os.close(base_fd)
        return cls(base_dir / name, child_fd)

    def child(self, name: str) -> SafeOutputDir:
        """Create and enter a subdirectory, refusing a symlinked component."""
        child_fd = _mkdir_open_nofollow(self._fd, name, self._path)
        return SafeOutputDir(self._path / name, child_fd)

    @property
    def path(self) -> Path:
        """The logical path of this directory (for logging and return values)."""
        return self._path

    def write_text(
        self, name: str, text: str, *, executable: bool = False
    ) -> Path:
        """Write UTF-8 *text* to *name*, refusing to follow a leaf symlink."""
        return self.write_bytes(
            name, text.encode("utf-8"), executable=executable
        )

    def write_bytes(
        self, name: str, data: bytes, *, executable: bool = False
    ) -> Path:
        """Write *data* to *name* under this directory, bound to the held fd.

        A pre-existing symlink, non-regular file (FIFO, device, directory), or
        hardlinked file at *name* is refused before any mutation; a lone
        regular file is truncated and overwritten, preserving the safe
        re-mining flow. Operational open failures (permission, disk full)
        propagate unchanged.
        """
        _require_component(name, self._path)
        mode = 0o755 if executable else 0o644
        try:
            fd = os.open(name, _FILE_FLAGS, mode, dir_fd=self._fd)
        except OSError as exc:
            if exc.errno in _CONTAINMENT_ERRNOS:
                raise ContainmentError(
                    f"refusing to write through symlink or non-regular path "
                    f"{name!r} under {self._path}"
                ) from exc
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ContainmentError(
                    f"refusing to write to non-regular file {name!r} "
                    f"under {self._path}"
                )
            if info.st_nlink != 1:
                # A hardlink shares its inode with another name (possibly
                # outside base_dir); truncating/writing it would corrupt that
                # target. O_NOFOLLOW does not catch hardlinks, so reject here.
                raise ContainmentError(
                    f"refusing to write through hardlinked file {name!r} "
                    f"under {self._path}"
                )
            # Truncate only AFTER validation (hence no O_TRUNC on open), so a
            # rejected target is never mutated.
            os.ftruncate(fd, 0)
            # O_CREAT only applies mode on creation. Force 0o755 for scripts
            # so an overwrite of a pre-existing verifier stays executable
            # (matching the writers' historical explicit chmod); leave
            # non-executable files' modes untouched on overwrite.
            if executable:
                os.fchmod(fd, mode)
            stream = os.fdopen(fd, "wb", closefd=True)
        except BaseException:
            # fd is not yet owned by a stream; close it exactly once here.
            # Once fdopen succeeds the ``with`` below owns it.
            os.close(fd)
            raise
        with stream:
            stream.write(data)
        return self._path / name

    def unlink(self, name: str, *, missing_ok: bool = False) -> None:
        """Remove a file or symlink *name*, bound to the held descriptor.

        A symlink is removed itself, never dereferenced.
        """
        _require_component(name, self._path)
        try:
            os.unlink(name, dir_fd=self._fd)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def remove_tree(self, name: str) -> None:
        """Recursively remove *name* under this directory, bound to the fd.

        A symlink or non-directory entry is unlinked (never dereferenced); a
        real directory is emptied and removed via fd-relative syscalls, so a
        swapped path string cannot redirect the removal outside this
        directory. A missing *name* is a no-op.
        """
        _require_component(name, self._path)
        try:
            _remove_tree_at(self._fd, name)
        except FileNotFoundError:
            pass

    def close(self) -> None:
        """Release the held directory descriptor (idempotent)."""
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __enter__(self) -> SafeOutputDir:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def _mkdir_open_nofollow(parent_fd: int, name: str, parent_path: Path) -> int:
    """Create *name* under *parent_fd* if absent, then open it O_NOFOLLOW.

    Returns the open directory descriptor. Raises :class:`ContainmentError`
    if *name* is a symlink or not a directory; operational ``mkdir``/``open``
    failures (permission, disk full) propagate unchanged.
    """
    _require_component(name, parent_path)
    try:
        os.mkdir(name, 0o755, dir_fd=parent_fd)
    except FileExistsError:
        pass
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        if exc.errno in _CONTAINMENT_ERRNOS:
            raise ContainmentError(
                f"refusing to descend into symlinked or non-directory "
                f"component {name!r} under {parent_path}"
            ) from exc
        raise


def _remove_tree_at(parent_fd: int, name: str) -> None:
    """fd-relative recursive removal of *name* under *parent_fd*.

    Uses ``lstat`` so a symlink is unlinked (not followed), and descends into
    real directories with ``O_NOFOLLOW`` descriptors so no component is
    resolved through a symlink.
    """
    info = os.lstat(name, dir_fd=parent_fd)
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    sub_fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    try:
        for entry in os.listdir(sub_fd):
            _remove_tree_at(sub_fd, entry)
    finally:
        os.close(sub_fd)
    os.rmdir(name, dir_fd=parent_fd)
