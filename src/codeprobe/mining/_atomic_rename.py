"""Descriptor-relative Linux rename primitives for mining publication."""

from __future__ import annotations

import ctypes
import errno
import os

_RENAME_NOREPLACE = 1


def rename_component(
    parent_fd: int,
    source: str,
    destination: str,
) -> None:
    """Rename one child within a descriptor-bound directory."""
    os.rename(
        source,
        destination,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
    )


def rename_component_no_replace(
    parent_fd: int,
    source: str,
    destination: str,
) -> None:
    """Atomically rename a child only when the destination is absent."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError as exc:
        raise OSError(
            errno.ENOSYS,
            "atomic no-replace directory rename is unavailable",
        ) from exc
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(destination),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            destination,
        )
