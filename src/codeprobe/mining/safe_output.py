"""Descriptor-bound, symlink-refusing writes for reused mining output trees.

Unlike immutable snapshot output, mining must overwrite task artifacts,
mark verifier scripts executable, and remove stale files. This module keeps
those operations beneath held ``O_NOFOLLOW`` directory descriptors so a
path swap cannot redirect reads, writes, or cleanup outside the trusted base.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from codeprobe.mining._atomic_rename import (
    rename_component as _rename_component,
)
from codeprobe.mining._atomic_rename import (
    rename_component_no_replace as _rename_component_no_replace,
)

__all__ = [
    "ContainmentError",
    "SafeOutputDir",
    "staged_output_dirs",
]

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

_STAGE_PREFIX = ".codeprobe-stage-"
_BACKUP_PREFIX = ".codeprobe-backup-"
_CLEANUP_PREFIX = ".codeprobe-cleanup-"


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


@dataclass(frozen=True)
class _StagedDirectory:
    final_name: str
    stage_name: str
    stage_identity: tuple[int, int]
    original_identity: tuple[int, int] | None


@dataclass(frozen=True)
class _PublishedSwap:
    entry: _StagedDirectory
    backup_name: str | None


@contextmanager
def staged_output_dirs(
    base_dir: Path,
    names: list[str] | tuple[str, ...],
) -> Iterator[Mapping[str, SafeOutputDir]]:
    """Stage complete directories and publish them as one rollback-safe batch.

    Every staged directory is a sibling of its final destination, so each
    descriptor-relative rename stays on the same filesystem. Existing
    destinations remain untouched while callers write. On successful exit,
    all staged trees are swapped into place; if a later swap fails, earlier
    swaps are reversed before the original exception propagates.

    Existing output trees are validated before staging so switching from
    in-place writes to whole-directory replacement does not weaken
    :class:`SafeOutputDir`'s symlink, wrong-type, or hardlink rejection.
    """
    base_dir = Path(base_dir)
    final_names = tuple(names)
    if len(set(final_names)) != len(final_names):
        raise ValueError("staged output names must be unique")
    for name in final_names:
        _require_component(name, base_dir)

    base_dir.mkdir(parents=True, exist_ok=True)
    base_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    base_fd = os.open(base_dir, base_flags)
    staged: list[_StagedDirectory] = []
    handles: dict[str, SafeOutputDir] = {}
    try:
        try:
            for final_name in final_names:
                identity = _validate_existing_tree(
                    base_fd,
                    name=final_name,
                    parent_path=base_dir,
                )
                stage_name, stage_fd = _create_private_directory(
                    base_fd,
                    _STAGE_PREFIX,
                    base_dir,
                )
                staged.append(
                    _StagedDirectory(
                        final_name=final_name,
                        stage_name=stage_name,
                        stage_identity=_directory_identity(stage_fd),
                        original_identity=identity,
                    )
                )
                handles[final_name] = SafeOutputDir(
                    base_dir / stage_name,
                    stage_fd,
                )
            yield handles
        except BaseException as exc:
            _close_handles(handles.values())
            cleanup_errors = _remove_staged_components(base_fd, staged)
            if cleanup_errors:
                raise BaseExceptionGroup(
                    "staged output preparation and cleanup both failed",
                    [exc, *cleanup_errors],
                ) from exc
            raise
        else:
            _close_handles(handles.values())
            _publish_staged_directories(base_fd, base_dir, staged)
    finally:
        _close_handles(handles.values())
        os.close(base_fd)


def _close_handles(handles: Iterable[SafeOutputDir]) -> None:
    """Close a collection of output handles."""
    for handle in handles:
        handle.close()


def _create_private_directory(
    parent_fd: int,
    prefix: str,
    parent_path: Path,
) -> tuple[str, int]:
    """Create and securely open an unpredictable private child directory."""
    for _ in range(10):
        name = f"{prefix}{secrets.token_hex(12)}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            continue
        try:
            return name, _open_dir_nofollow(parent_fd, name, parent_path)
        except BaseException:
            # The component may have been replaced between mkdir and open.
            # Without a retained descriptor identity, deleting it could remove
            # unrelated concurrent data. Preserve it for recovery.
            raise
    raise FileExistsError(
        f"could not allocate private output directory under {parent_path}"
    )


def _validate_existing_tree(
    parent_fd: int,
    name: str,
    parent_path: Path,
) -> tuple[int, int] | None:
    """Validate an existing destination and return its stable root identity."""
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise ContainmentError(
            f"refusing to replace symlinked or non-directory component "
            f"{name!r} under {parent_path}"
        )
    child_fd = _open_dir_nofollow(parent_fd, name, parent_path)
    try:
        _validate_tree_contents(child_fd, parent_path / name)
        opened = os.fstat(child_fd)
        return opened.st_dev, opened.st_ino
    finally:
        os.close(child_fd)


def _validate_tree_contents(directory_fd: int, directory_path: Path) -> None:
    """Reject symlinks, special files, and hardlinks in an existing tree."""
    for name in os.listdir(directory_fd):
        info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            child_fd = _open_dir_nofollow(
                directory_fd,
                name,
                directory_path,
            )
            try:
                _validate_tree_contents(child_fd, directory_path / name)
            finally:
                os.close(child_fd)
            continue
        if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            continue
        raise ContainmentError(
            f"refusing unsafe existing entry {name!r} under {directory_path}"
        )


def _publish_staged_directories(
    parent_fd: int,
    parent_path: Path,
    staged: list[_StagedDirectory],
) -> None:
    """Publish every staged tree, rolling all swaps back on a later failure."""
    swaps: list[_PublishedSwap] = []
    try:
        for entry in staged:
            _require_stage_ready(parent_fd, parent_path, entry)
            _require_original_identity(parent_fd, parent_path, entry)
            backup_name: str | None = None
            if entry.original_identity is not None:
                backup_name = _unused_private_name(parent_fd, _BACKUP_PREFIX)
            swaps.append(
                _PublishedSwap(
                    entry=entry,
                    backup_name=backup_name,
                )
            )
            if backup_name is not None:
                _rename_component(
                    parent_fd,
                    entry.final_name,
                    backup_name,
                )
            _rename_component_no_replace(
                parent_fd,
                entry.stage_name,
                entry.final_name,
            )
            _require_published_ready(parent_fd, parent_path, entry)
    except BaseException as exc:
        rollback_errors = _rollback_swaps(parent_fd, swaps)
        cleanup_errors = _remove_staged_components(parent_fd, staged)
        recovery_errors = [*rollback_errors, *cleanup_errors]
        if recovery_errors:
            raise BaseExceptionGroup(
                "output publication and rollback both failed",
                [exc, *recovery_errors],
            ) from exc
        raise

    cleanup_errors = _remove_backup_components(parent_fd, swaps)
    if cleanup_errors:
        raise BaseExceptionGroup(
            "output published but backup cleanup failed",
            cleanup_errors,
        )


def _require_stage_ready(
    parent_fd: int,
    parent_path: Path,
    entry: _StagedDirectory,
) -> None:
    """Verify the staged root identity and reject unsafe staged contents."""
    _require_named_tree_ready(
        parent_fd,
        parent_path,
        entry.stage_name,
        entry.stage_identity,
        description=f"staged output for {entry.final_name!r}",
    )


def _require_published_ready(
    parent_fd: int,
    parent_path: Path,
    entry: _StagedDirectory,
) -> None:
    """Verify that publication renamed the validated staged tree."""
    _require_named_tree_ready(
        parent_fd,
        parent_path,
        entry.final_name,
        entry.stage_identity,
        description=f"published output for {entry.final_name!r}",
    )


def _require_named_tree_ready(
    parent_fd: int,
    parent_path: Path,
    name: str,
    expected_identity: tuple[int, int],
    *,
    description: str,
) -> None:
    """Verify one named directory's identity and recursively safe contents."""
    try:
        directory_fd = _open_dir_nofollow(
            parent_fd,
            name,
            parent_path,
        )
    except FileNotFoundError:
        raise ContainmentError(
            f"{description} changed under {parent_path}"
        ) from None
    try:
        if _directory_identity(directory_fd) != expected_identity:
            raise ContainmentError(
                f"{description} changed under {parent_path}"
            )
        _validate_tree_contents(directory_fd, parent_path / name)
    finally:
        os.close(directory_fd)


def _directory_identity(directory_fd: int) -> tuple[int, int]:
    """Return the device/inode identity for an open directory descriptor."""
    info = os.fstat(directory_fd)
    return info.st_dev, info.st_ino


def _require_original_identity(
    parent_fd: int,
    parent_path: Path,
    entry: _StagedDirectory,
) -> None:
    """Reject a destination root changed between validation and publication."""
    try:
        info = os.stat(
            entry.final_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        if entry.original_identity is None:
            return
        raise ContainmentError(
            f"output component {entry.final_name!r} changed under {parent_path}"
        ) from None

    identity = (info.st_dev, info.st_ino)
    if (
        not stat.S_ISDIR(info.st_mode)
        or entry.original_identity is None
        or identity != entry.original_identity
    ):
        raise ContainmentError(
            f"output component {entry.final_name!r} changed under {parent_path}"
        )


def _rollback_swaps(
    parent_fd: int,
    swaps: list[_PublishedSwap],
) -> list[BaseException]:
    """Restore original destinations in reverse publication order."""
    errors: list[BaseException] = []
    for swap in reversed(swaps):
        try:
            entry = swap.entry
            final_identity = _component_identity(
                parent_fd,
                entry.final_name,
            )
            original_at_final = (
                entry.original_identity is not None
                and final_identity == entry.original_identity
            )
            if final_identity is not None and not original_at_final:
                if final_identity != entry.stage_identity:
                    raise ContainmentError(
                        f"cannot safely reconcile unexpected output "
                        f"component {entry.final_name!r}"
                    )
                if _component_identity(parent_fd, entry.stage_name) is not None:
                    raise ContainmentError(
                        f"cannot safely reconcile output component "
                        f"{entry.final_name!r}"
                    )
                _rename_component(
                    parent_fd,
                    entry.final_name,
                    entry.stage_name,
                )
            if swap.backup_name is not None:
                backup_identity = _component_identity(
                    parent_fd,
                    swap.backup_name,
                )
                if backup_identity is not None:
                    if backup_identity != entry.original_identity:
                        raise ContainmentError(
                            f"cannot safely reconcile backup for "
                            f"{entry.final_name!r}"
                        )
                    _rename_component(
                        parent_fd,
                        swap.backup_name,
                        entry.final_name,
                    )
                elif not original_at_final:
                    raise ContainmentError(
                        f"original output for {entry.final_name!r} is missing"
                    )
        except BaseException as exc:
            errors.append(exc)
    return errors


def _component_identity(
    parent_fd: int,
    name: str,
) -> tuple[int, int] | None:
    """Return a child component's inode identity without following symlinks."""
    try:
        info = os.stat(
            name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return None
    return info.st_dev, info.st_ino


def _unused_private_name(parent_fd: int, prefix: str) -> str:
    """Return an unpredictable child name that is absent under *parent_fd*."""
    for _ in range(10):
        name = f"{prefix}{secrets.token_hex(12)}"
        try:
            os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return name
    raise FileExistsError("could not allocate private backup name")


def _remove_staged_components(
    parent_fd: int,
    staged: Iterable[_StagedDirectory],
) -> list[BaseException]:
    """Remove only stage components whose retained identities still match."""
    errors: list[BaseException] = []
    for entry in staged:
        try:
            _remove_owned_component(
                parent_fd,
                entry.stage_name,
                entry.stage_identity,
            )
        except BaseException as exc:
            errors.append(exc)
    return errors


def _remove_backup_components(
    parent_fd: int,
    swaps: Iterable[_PublishedSwap],
) -> list[BaseException]:
    """Remove only backups whose identities match their original outputs."""
    errors: list[BaseException] = []
    for swap in swaps:
        original_identity = swap.entry.original_identity
        if swap.backup_name is None or original_identity is None:
            continue
        try:
            _remove_owned_component(
                parent_fd,
                swap.backup_name,
                original_identity,
            )
        except BaseException as exc:
            errors.append(exc)
    return errors


def _remove_owned_component(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
) -> None:
    """Atomically claim, revalidate, and remove a transaction-owned directory."""
    quarantine_name = _unused_private_name(parent_fd, _CLEANUP_PREFIX)
    try:
        _rename_component_no_replace(
            parent_fd,
            name,
            quarantine_name,
        )
    except FileNotFoundError:
        return

    claimed_identity = _component_identity(parent_fd, quarantine_name)
    if claimed_identity != expected_identity:
        conflict = ContainmentError(
            f"refusing to clean changed transaction component {name!r}"
        )
        try:
            _rename_component_no_replace(
                parent_fd,
                quarantine_name,
                name,
            )
        except BaseException as restore_exc:
            raise BaseExceptionGroup(
                "cleanup claim changed and restoration failed",
                [conflict, restore_exc],
            ) from conflict
        raise conflict

    _remove_claimed_directory(
        parent_fd,
        quarantine_name,
        expected_identity,
    )


def _remove_claimed_directory(
    parent_fd: int,
    name: str,
    expected_identity: tuple[int, int],
) -> None:
    """Empty a claimed directory through its verified descriptor, then remove it."""
    directory_fd = _open_dir_nofollow(parent_fd, name, Path(name).parent)
    try:
        if _directory_identity(directory_fd) != expected_identity:
            raise ContainmentError(
                f"refusing to clean changed transaction component {name!r}"
            )
        os.fchmod(directory_fd, 0o700)
        for entry in os.listdir(directory_fd):
            _remove_tree_at(directory_fd, entry)
    finally:
        os.close(directory_fd)

    if _component_identity(parent_fd, name) != expected_identity:
        raise ContainmentError(
            f"refusing to clean changed transaction component {name!r}"
        )
    os.rmdir(name, dir_fd=parent_fd)


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
    return _open_dir_nofollow(parent_fd, name, parent_path)


def _open_dir_nofollow(
    parent_fd: int,
    name: str,
    parent_path: Path,
) -> int:
    """Open a child directory and preserve the containment error contract."""
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
