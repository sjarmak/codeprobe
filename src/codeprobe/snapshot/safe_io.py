"""Race-resistant filesystem primitives for snapshot source and output trees."""

from __future__ import annotations

import ctypes
import errno
import os
import secrets
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePath
from types import TracebackType


class SymlinkEscapeError(RuntimeError):
    """Raised when snapshot I/O cannot prove that a path stays contained."""


@dataclass(frozen=True)
class SourceFile:
    """Source bytes captured through a directory-relative, no-follow open."""

    relative_path: str
    body: bytes


MAX_SOURCE_CAPTURE_BYTES = 256 * 1024 * 1024


_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_FILE_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(
    os, "O_NOFOLLOW", 0
)
_FILE_WRITE_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_SECURE_PRIMITIVES_SUPPORTED = (
    os.name == "posix"
    and all(hasattr(os, flag) for flag in ("O_DIRECTORY", "O_NOFOLLOW"))
    and all(
        function in os.supports_dir_fd
        for function in (os.open, os.mkdir, os.stat, os.symlink)
    )
    and os.stat in os.supports_follow_symlinks
    and os.listdir in os.supports_fd
)


def _require_secure_primitives() -> None:
    if not _SECURE_PRIMITIVES_SUPPORTED:
        raise SymlinkEscapeError(
            "secure snapshot filesystem operations are unsupported on this platform"
        )


def _absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _open_directory(path: Path, *, create: bool) -> tuple[Path, int]:
    """Open every component relative to a pinned parent directory descriptor."""
    _require_secure_primitives()
    absolute = _absolute_path(path)
    current_fd = os.open(os.sep, _DIRECTORY_FLAGS)
    try:
        for component in absolute.parts[1:]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
                except OSError as exc:
                    raise SymlinkEscapeError(
                        "snapshot output path cannot be created securely"
                    ) from exc
            try:
                child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=current_fd)
            except FileNotFoundError:
                if not create:
                    raise FileNotFoundError(
                        f"snapshot source_dir does not exist: {absolute}"
                    ) from None
                raise
            except OSError as exc:
                raise SymlinkEscapeError(
                    "snapshot path contains a symlink or non-directory component"
                ) from exc
            os.close(current_fd)
            current_fd = child_fd
        return absolute, current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _skip_parts(source: Path, output: Path | None) -> tuple[str, ...] | None:
    if output is None:
        return None
    source_absolute = _absolute_path(source)
    output_absolute = _absolute_path(output)
    try:
        relative = output_absolute.relative_to(source_absolute)
    except ValueError:
        return None
    if not relative.parts:
        raise SymlinkEscapeError(
            "snapshot source and output directories must be different"
        )
    return relative.parts


def read_source_files(
    source_dir: Path,
    *,
    output_dir: Path | None = None,
) -> tuple[Path, list[SourceFile], list[str]]:
    """Capture regular files without following a source path or entry symlink."""
    source_absolute, source_fd = _open_directory(source_dir, create=False)
    skipped = _skip_parts(source_absolute, output_dir)
    files: list[SourceFile] = []
    directories: list[str] = []
    captured_bytes = 0

    def walk(directory_fd: int, parent_parts: tuple[str, ...]) -> None:
        nonlocal captured_bytes
        for name in sorted(os.listdir(directory_fd)):
            relative_parts = (*parent_parts, name)
            if skipped is not None and relative_parts[: len(skipped)] == skipped:
                continue
            try:
                metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise SymlinkEscapeError(
                    "snapshot source changed during secure traversal"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise SymlinkEscapeError("snapshot source contains a symlink")
            if stat.S_ISDIR(metadata.st_mode):
                directories.append(PurePath(*relative_parts).as_posix())
                try:
                    child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
                except OSError as exc:
                    raise SymlinkEscapeError(
                        "snapshot source directory changed during secure traversal"
                    ) from exc
                try:
                    opened_metadata = os.fstat(child_fd)
                    if (
                        metadata.st_dev,
                        metadata.st_ino,
                    ) != (
                        opened_metadata.st_dev,
                        opened_metadata.st_ino,
                    ):
                        raise SymlinkEscapeError(
                            "snapshot source directory changed during secure traversal"
                        )
                    walk(child_fd, relative_parts)
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise SymlinkEscapeError(
                    "snapshot source contains an unsupported special file"
                )
            try:
                file_fd = os.open(name, _FILE_READ_FLAGS, dir_fd=directory_fd)
            except OSError as exc:
                raise SymlinkEscapeError(
                    "snapshot source file changed during secure traversal"
                ) from exc
            try:
                opened_metadata = os.fstat(file_fd)
                if not stat.S_ISREG(opened_metadata.st_mode):
                    raise SymlinkEscapeError(
                        "snapshot source entry is not a regular file"
                    )
                if (
                    metadata.st_dev,
                    metadata.st_ino,
                ) != (
                    opened_metadata.st_dev,
                    opened_metadata.st_ino,
                ):
                    raise SymlinkEscapeError(
                        "snapshot source file changed during secure traversal"
                    )
                source = os.fdopen(file_fd, "rb")
                file_fd = -1
                with source:
                    remaining = MAX_SOURCE_CAPTURE_BYTES - captured_bytes
                    body = source.read(remaining + 1)
                    if len(body) > remaining:
                        raise SymlinkEscapeError(
                            "snapshot source exceeds the capture size limit"
                        )
                    captured_bytes += len(body)
            finally:
                if file_fd >= 0:
                    os.close(file_fd)
            files.append(
                SourceFile(
                    relative_path=PurePath(*relative_parts).as_posix(),
                    body=body,
                )
            )

    try:
        walk(source_fd, ())
    finally:
        os.close(source_fd)
    return source_absolute, files, directories


def validate_source_tree(source_dir: Path) -> None:
    """Reject symlinks anywhere in a source path or tree without reading bodies."""
    _, source_fd = _open_directory(source_dir, create=False)

    def walk(directory_fd: int) -> None:
        for name in sorted(os.listdir(directory_fd)):
            try:
                metadata = os.stat(
                    name,
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except OSError as exc:
                raise SymlinkEscapeError(
                    "snapshot source changed during secure traversal"
                ) from exc
            if stat.S_ISLNK(metadata.st_mode):
                raise SymlinkEscapeError("snapshot source symlinks are unsupported")
            if stat.S_ISREG(metadata.st_mode):
                continue
            if not stat.S_ISDIR(metadata.st_mode):
                raise SymlinkEscapeError(
                    "snapshot source contains an unsupported special file"
                )
            try:
                child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            except OSError as exc:
                raise SymlinkEscapeError(
                    "snapshot source directory changed during secure traversal"
                ) from exc
            try:
                opened_metadata = os.fstat(child_fd)
                if (
                    metadata.st_dev,
                    metadata.st_ino,
                ) != (
                    opened_metadata.st_dev,
                    opened_metadata.st_ino,
                ):
                    raise SymlinkEscapeError(
                        "snapshot source directory changed during secure traversal"
                    )
                walk(child_fd)
            finally:
                os.close(child_fd)

    try:
        walk(source_fd)
    finally:
        os.close(source_fd)


def read_regular_file(
    root: Path,
    relative_path: str,
    *,
    max_bytes: int,
) -> bytes:
    """Read one bounded regular file relative to a no-follow directory root."""
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    _, root_fd = _open_directory(root, create=False)
    parts = _validated_relative_parts(relative_path)
    parent_fd = root_fd
    file_fd = -1
    try:
        for component in parts[:-1]:
            try:
                child_fd = os.open(component, _DIRECTORY_FLAGS, dir_fd=parent_fd)
            except OSError as exc:
                raise SymlinkEscapeError(
                    "snapshot input path contains a symlink or non-directory"
                ) from exc
            os.close(parent_fd)
            parent_fd = child_fd
        try:
            file_fd = os.open(
                parts[-1],
                _FILE_READ_FLAGS | getattr(os, "O_NONBLOCK", 0),
                dir_fd=parent_fd,
            )
        except OSError as exc:
            raise SymlinkEscapeError(
                "snapshot input file cannot be opened securely"
            ) from exc
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise SymlinkEscapeError("snapshot input file is not a regular file")
        with os.fdopen(file_fd, "rb") as source:
            file_fd = -1
            body = source.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise SymlinkEscapeError("snapshot input file exceeds its size limit")
        return body
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        os.close(parent_fd)


def _validated_relative_parts(relative_path: str) -> tuple[str, ...]:
    path = PurePath(relative_path)
    if path.is_absolute() or not path.parts or any(
        part in ("", ".", "..") for part in path.parts
    ):
        raise SymlinkEscapeError("snapshot output path is not a safe relative path")
    return path.parts


class SecureOutputDirectory:
    """Exclusive writer rooted at a descriptor-pinned output directory."""

    def __init__(self, path: Path, *, opened_fd: int | None = None) -> None:
        self.path = _absolute_path(path)
        self._fd = opened_fd
        self._directory_identities: dict[tuple[str, ...], tuple[int, int]] = {}
        self._file_identities: dict[tuple[str, ...], tuple[int, int]] = {}
        self._symlink_identities: dict[tuple[str, ...], tuple[int, int]] = {}

    def __enter__(self) -> SecureOutputDirectory:
        if self._fd is None:
            _, self._fd = _open_directory(self.path, create=True)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_value, traceback
        close_error: OSError | None = None
        self._directory_identities = {}
        self._file_identities = {}
        self._symlink_identities = {}
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError as exc:
                if close_error is None:
                    close_error = exc
            self._fd = None
        if close_error is not None and exc_type is None:
            raise SymlinkEscapeError(
                "snapshot output descriptors could not be closed safely"
            ) from close_error

    def _open_directory_parts(
        self,
        parts: tuple[str, ...],
        *,
        create: bool,
    ) -> int:
        assert self._fd is not None
        parent_fd = os.dup(self._fd)
        try:
            for index, component in enumerate(parts):
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                    except FileExistsError:
                        pass
                    except OSError as exc:
                        raise SymlinkEscapeError(
                            "snapshot output parent cannot be created securely"
                        ) from exc
                try:
                    child_fd = os.open(
                        component,
                        _DIRECTORY_FLAGS,
                        dir_fd=parent_fd,
                    )
                except OSError as exc:
                    raise SymlinkEscapeError(
                        "snapshot output parent contains a symlink"
                    ) from exc
                key = parts[: index + 1]
                os.close(parent_fd)
                parent_fd = child_fd
                metadata = os.fstat(parent_fd)
                identity = (metadata.st_dev, metadata.st_ino)
                pinned_identity = self._directory_identities.get(key)
                if pinned_identity is not None and pinned_identity != identity:
                    raise SymlinkEscapeError(
                        "snapshot output directory changed during secure write"
                    )
                self._directory_identities[key] = identity
            return parent_fd
        except BaseException:
            os.close(parent_fd)
            raise

    def _open_parent(self, parts: tuple[str, ...]) -> int:
        return self._open_directory_parts(parts, create=True)

    def write_bytes(self, relative_path: str, data: bytes) -> Path:
        """Create one output file without following parents or the leaf."""
        if self._fd is None:
            raise RuntimeError("SecureOutputDirectory must be entered before use")
        parts = _validated_relative_parts(relative_path)
        parent_fd = self._open_parent(parts[:-1])
        try:
            file_fd = -1
            try:
                file_fd = os.open(
                    parts[-1],
                    _FILE_WRITE_FLAGS,
                    0o600,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                message = (
                    "snapshot output file already exists or cannot be created securely"
                    if exc.errno in (errno.EEXIST, errno.ELOOP)
                    else "snapshot output file cannot be created securely"
                )
                raise SymlinkEscapeError(message) from exc
            try:
                view = memoryview(data)
                while view:
                    written = os.write(file_fd, view)
                    if written == 0:
                        raise OSError("short write while creating snapshot output")
                    view = view[written:]
            except OSError as exc:
                raise SymlinkEscapeError(
                    "snapshot output file could not be written securely"
                ) from exc
            else:
                metadata = os.fstat(file_fd)
                self._file_identities[parts] = (
                    metadata.st_dev,
                    metadata.st_ino,
                )
            finally:
                if file_fd >= 0:
                    os.close(file_fd)
        finally:
            os.close(parent_fd)
        return self.path.joinpath(*parts)

    def ensure_directory(self, relative_path: str) -> Path:
        """Create and pin an output directory below the secure root."""
        parts = _validated_relative_parts(relative_path)
        directory_fd = self._open_parent(parts)
        os.close(directory_fd)
        return self.path.joinpath(*parts)

    def symlink(self, relative_path: str, target: str) -> Path:
        """Create one relative symlink beneath pinned output parents."""
        if self._fd is None:
            raise RuntimeError("SecureOutputDirectory must be entered before use")
        target_path = PurePath(target)
        if target_path.is_absolute():
            raise SymlinkEscapeError("snapshot symlink target must be relative")
        parts = _validated_relative_parts(relative_path)
        parent_fd = self._open_parent(parts[:-1])
        try:
            try:
                os.symlink(target, parts[-1], dir_fd=parent_fd)
            except OSError as exc:
                raise SymlinkEscapeError(
                    "snapshot output symlink cannot be created securely"
                ) from exc
            metadata = os.stat(
                parts[-1],
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            self._symlink_identities[parts] = (
                metadata.st_dev,
                metadata.st_ino,
            )
        finally:
            os.close(parent_fd)
        return self.path.joinpath(*parts)

    def ensure_path_unchanged(self) -> None:
        """Fail if the pathname no longer resolves to the pinned root inode."""
        if self._fd is None:
            raise RuntimeError("SecureOutputDirectory must be entered before use")
        try:
            _, path_fd = _open_directory(self.path, create=False)
        except (FileNotFoundError, SymlinkEscapeError) as exc:
            raise SymlinkEscapeError(
                "snapshot output root changed during secure write"
            ) from exc
        try:
            pinned = os.fstat(self._fd)
            current = os.fstat(path_fd)
        finally:
            os.close(path_fd)
        if (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino):
            raise SymlinkEscapeError(
                "snapshot output root changed during secure write"
            )
        for parts, pinned_identity in sorted(
            self._directory_identities.items(),
            key=lambda item: len(item[0]),
        ):
            try:
                visible_fd = self._open_directory_parts(parts, create=False)
            except SymlinkEscapeError as exc:
                raise SymlinkEscapeError(
                    "snapshot output directory changed during secure write"
                ) from exc
            try:
                visible_directory = os.fstat(visible_fd)
            finally:
                os.close(visible_fd)
            if pinned_identity != (
                visible_directory.st_dev,
                visible_directory.st_ino,
            ):
                raise SymlinkEscapeError(
                    "snapshot output directory changed during secure write"
                )
        for parts, pinned_identity in self._file_identities.items():
            try:
                parent_fd = self._open_directory_parts(parts[:-1], create=False)
                try:
                    visible_fd = os.open(
                        parts[-1],
                        _FILE_READ_FLAGS,
                        dir_fd=parent_fd,
                    )
                finally:
                    os.close(parent_fd)
            except (OSError, SymlinkEscapeError) as exc:
                raise SymlinkEscapeError(
                    "snapshot output file changed during secure write"
                ) from exc
            try:
                visible_file = os.fstat(visible_fd)
            finally:
                os.close(visible_fd)
            if pinned_identity != (
                visible_file.st_dev,
                visible_file.st_ino,
            ):
                raise SymlinkEscapeError(
                    "snapshot output file changed during secure write"
                )
        for parts, pinned_identity in self._symlink_identities.items():
            try:
                parent_fd = self._open_directory_parts(parts[:-1], create=False)
                try:
                    visible_link = os.stat(
                        parts[-1],
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                finally:
                    os.close(parent_fd)
            except (OSError, SymlinkEscapeError) as exc:
                raise SymlinkEscapeError(
                    "snapshot output symlink changed during secure write"
                ) from exc
            if not stat.S_ISLNK(visible_link.st_mode) or pinned_identity != (
                visible_link.st_dev,
                visible_link.st_ino,
            ):
                raise SymlinkEscapeError(
                    "snapshot output symlink changed during secure write"
                )


def _path_identity(path: Path) -> tuple[int, int]:
    _, path_fd = _open_directory(path, create=False)
    try:
        metadata = os.fstat(path_fd)
        return metadata.st_dev, metadata.st_ino
    finally:
        os.close(path_fd)


def _destination_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SymlinkEscapeError(
            "snapshot output destination cannot be inspected securely"
        ) from exc
    return True


def _remove_owned_directory(
    parent_fd: int,
    identity: tuple[int, int],
) -> bool:
    for name in os.listdir(parent_fd):
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if (
            stat.S_ISDIR(metadata.st_mode)
            and (metadata.st_dev, metadata.st_ino) == identity
        ):
            os.rmdir(name, dir_fd=parent_fd)
            return True
    return False


def _erase_directory_contents(directory_fd: int) -> None:
    """Erase a pinned directory tree even if its pathname was moved."""
    for name in os.listdir(directory_fd):
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode):
            child_fd = os.open(name, _DIRECTORY_FLAGS, dir_fd=directory_fd)
            try:
                opened = os.fstat(child_fd)
                if (metadata.st_dev, metadata.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    raise SymlinkEscapeError(
                        "snapshot staging tree changed during secure cleanup"
                    )
                _erase_directory_contents(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def _rename_noreplace(
    source_name: str,
    destination_name: str,
    *,
    parent_fd: int,
) -> None:
    """Atomically publish without replacing a raced destination."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError:
        raise SymlinkEscapeError(
            "atomic no-replace snapshot publication is unsupported"
        ) from None
    result = renameat2(
        parent_fd,
        os.fsencode(source_name),
        parent_fd,
        os.fsencode(destination_name),
        1,  # RENAME_NOREPLACE
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise SymlinkEscapeError("snapshot output destination already exists")
    if error_number in (errno.ENOSYS, errno.EINVAL):
        raise SymlinkEscapeError(
            "atomic no-replace snapshot publication is unsupported"
        )
    raise SymlinkEscapeError(
        "snapshot output could not be published atomically"
    ) from OSError(error_number, os.strerror(error_number))


@contextmanager
def staged_output_directory(final_path: Path) -> Iterator[SecureOutputDirectory]:
    """Build an owned sibling tree and atomically publish it on success."""
    final_absolute = _absolute_path(final_path)
    if final_absolute == Path(os.sep) or not final_absolute.name:
        raise SymlinkEscapeError("snapshot output destination is invalid")
    parent_path = final_absolute.parent
    _, parent_fd = _open_directory(parent_path, create=True)
    parent_identity = os.fstat(parent_fd)
    stage_name = f".{final_absolute.name}.tmp-{secrets.token_hex(12)}"
    stage_created = False
    stage_identity: tuple[int, int] | None = None
    cleanup_fd = -1
    try:
        if _destination_exists(parent_fd, final_absolute.name):
            raise SymlinkEscapeError("snapshot output destination already exists")
        os.mkdir(stage_name, mode=0o700, dir_fd=parent_fd)
        stage_created = True
        stage_fd = os.open(stage_name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        cleanup_fd = os.dup(stage_fd)
        stage_metadata = os.fstat(stage_fd)
        stage_identity = (stage_metadata.st_dev, stage_metadata.st_ino)
        stage_path = parent_path / stage_name
        with SecureOutputDirectory(stage_path, opened_fd=stage_fd) as output:
            yield output
            output.ensure_path_unchanged()

        if (parent_identity.st_dev, parent_identity.st_ino) != _path_identity(
            parent_path
        ):
            raise SymlinkEscapeError(
                "snapshot output parent changed before atomic publication"
            )
        _rename_noreplace(
            stage_name,
            final_absolute.name,
            parent_fd=parent_fd,
        )
        stage_created = False
    finally:
        cleanup_error: BaseException | None = None
        if stage_created:
            try:
                assert stage_identity is not None
                assert cleanup_fd >= 0
                _erase_directory_contents(cleanup_fd)
                removed = _remove_owned_directory(parent_fd, stage_identity)
                if not removed or os.fstat(cleanup_fd).st_nlink != 0:
                    raise SymlinkEscapeError(
                        "moved snapshot staging root could not be removed"
                    )
            except (OSError, SymlinkEscapeError) as exc:
                cleanup_error = exc
        if cleanup_fd >= 0:
            os.close(cleanup_fd)
        os.close(parent_fd)
        if cleanup_error is not None:
            raise SymlinkEscapeError(
                "snapshot staging directory could not be cleaned securely"
            ) from cleanup_error
