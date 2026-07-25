"""Apply scanner resource limits before replacing this process with the scanner."""

from __future__ import annotations

import os
import resource
import sys
from collections.abc import Sequence


def _apply_file_size_limit(output_limit: int) -> None:
    _, hard_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
    effective_limit = (
        output_limit
        if hard_limit == resource.RLIM_INFINITY
        else min(output_limit, hard_limit)
    )
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (effective_limit, hard_limit),
    )


def _main(arguments: Sequence[str]) -> int:
    if len(arguments) < 3:
        return 2
    try:
        output_limit = int(arguments[0])
        pid_write_fd = int(arguments[1])
    except ValueError:
        return 2
    if output_limit <= 0 or pid_write_fd < 0:
        return 2

    scanner_args = list(arguments[2:])
    try:
        _apply_file_size_limit(output_limit)
        os.write(pid_write_fd, str(os.getpid()).encode())
    finally:
        os.close(pid_write_fd)
    os.execvpe(scanner_args[0], scanner_args, os.environ)
    return 127  # pragma: no cover - os.execvpe replaces this process


if __name__ == "__main__":  # pragma: no branch - script entry point
    raise SystemExit(_main(sys.argv[1:]))
