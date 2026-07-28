"""Descriptor-pinned reads for the flat evidence-bundle directory."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType

from codeprobe.snapshot.safe_io import (
    SymlinkEscapeError,
    _inventory_regular_file,
    _inventory_version,
    _open_directory,
)


def _require_exact_names(
    directory_fd: int,
    expected: frozenset[str],
) -> None:
    observed: set[str] = set()
    try:
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                if entry.name not in expected or len(observed) == len(expected):
                    raise SymlinkEscapeError(
                        "evidence directory must contain exactly "
                        "the expected regular files"
                    )
                observed.add(entry.name)
    except OSError as error:
        raise SymlinkEscapeError(
            "evidence directory changed during secure validation"
        ) from error
    if observed != expected:
        raise SymlinkEscapeError(
            "evidence directory must contain exactly the expected regular files"
        )


def _capture_regular_file(
    directory_fd: int,
    name: str,
    *,
    max_bytes: int,
) -> bytes:
    try:
        metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except OSError as error:
        raise SymlinkEscapeError(
            "evidence artifact changed during secure validation"
        ) from error
    if not stat.S_ISREG(metadata.st_mode):
        raise SymlinkEscapeError("evidence artifact is not a regular file")
    entry, _ = _inventory_regular_file(
        directory_fd,
        name,
        metadata,
        name,
        capture=True,
        max_capture_bytes=max_bytes,
        max_file_bytes=max_bytes,
    )
    if entry.body is None:
        raise SymlinkEscapeError("evidence artifact could not be captured")
    return entry.body


def read_exact_evidence_directory(
    root: Path,
    filenames: tuple[str, ...],
    *,
    max_artifact_bytes: int,
) -> Mapping[str, bytes]:
    """Read exact flat regular files without traversing unexpected entries."""
    if max_artifact_bytes <= 0:
        raise ValueError("evidence artifact size limit must be positive")
    expected = frozenset(filenames)
    if len(expected) != len(filenames) or any(
        not name or Path(name).name != name for name in filenames
    ):
        raise ValueError("evidence artifact names must be unique flat names")
    _, root_fd = _open_directory(root, create=False)
    try:
        directory_version = _inventory_version(os.fstat(root_fd))
        _require_exact_names(root_fd, expected)
        captured = MappingProxyType(
            {
                name: _capture_regular_file(
                    root_fd,
                    name,
                    max_bytes=max_artifact_bytes,
                )
                for name in filenames
            }
        )
        if directory_version != _inventory_version(os.fstat(root_fd)):
            raise SymlinkEscapeError(
                "evidence directory changed during secure validation"
            )
        return captured
    finally:
        os.close(root_fd)


__all__ = ["read_exact_evidence_directory"]
