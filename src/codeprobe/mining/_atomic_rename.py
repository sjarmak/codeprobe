"""Descriptor-relative rename primitives for mining publication."""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from typing import Final

# Each platform spells "rename, but fail if the destination exists"
# differently. Linux has renameat2(2) with RENAME_NOREPLACE (3.15+);
# Darwin has renameatx_np(2) with RENAME_EXCL (10.12+). Both take
# (fromfd, from, tofd, to, flags) and both report EEXIST on conflict, so
# only the symbol name and the flag value differ.
_NO_REPLACE_RENAME: Final[dict[str, tuple[str, int]]] = {
    "linux": ("renameat2", 0x1),
    "darwin": ("renameatx_np", 0x4),
}


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
    symbol, flags = _no_replace_syscall(sys.platform)
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        rename = getattr(libc, symbol)
    except AttributeError as exc:
        raise OSError(
            errno.ENOSYS,
            "atomic no-replace directory rename is unavailable: "
            f"libc exposes no {symbol}",
        ) from exc
    rename.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename.restype = ctypes.c_int
    result = rename(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(destination),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(
            error_number,
            os.strerror(error_number),
            destination,
        )


def _no_replace_syscall(platform: str) -> tuple[str, int]:
    """Return the libc symbol and no-replace flag for *platform*."""
    try:
        return _NO_REPLACE_RENAME[platform]
    except KeyError:
        raise OSError(
            errno.ENOSYS,
            "atomic no-replace directory rename is unsupported on "
            f"{platform}; codeprobe mining requires Linux or macOS",
        ) from None
