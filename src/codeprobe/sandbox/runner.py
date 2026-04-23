"""Container-based sandbox runner (INV4).

Executes a command inside a docker (preferred) or podman container with
host paths bind-mounted. Mounts are ``:ro`` by default, so the default
invocation cannot mutate the host worktree. The caller opts in to write
access by passing ``allow_writes=True``.

Design notes
------------

- Orchestration-only: this module is pure plumbing. It builds an argv,
  invokes the engine via :mod:`subprocess`, captures stdout/stderr, and
  translates known failure modes into exceptions. It makes no semantic
  judgments about the command being run.
- The engine is detected once per call via :func:`shutil.which`; docker
  wins when both are installed. Missing engine is a hard error — no
  silent fallback to host execution.
- Read-only mount violations are the one error class promoted to an
  exception so callers can distinguish "the sandbox prevented a write"
  from "the command exited non-zero". Every other non-zero exit is
  returned in :class:`SandboxResult` for the caller to inspect.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Final

logger = logging.getLogger(__name__)


DEFAULT_IMAGE: Final[str] = "codeprobe-sandbox:sg-only"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0

# Lower-cased stderr fragments that indicate a write to a read-only mount.
# Kept explicit because the exact wording varies between docker, podman, and
# the underlying kernel, but these three substrings cover all observed cases.
_RO_WRITE_STDERR_PATTERNS: Final[tuple[str, ...]] = (
    "read-only file system",
    "read only file system",
    "permission denied",
)


@dataclass(frozen=True)
class SandboxResult:
    """Captured output from a completed sandbox run."""

    stdout: str
    stderr: str
    exit_code: int
    duration_ms: int


class SandboxError(RuntimeError):
    """Base class for sandbox-runner failures (engine missing, timeout, etc.)."""


class SandboxWriteDenied(SandboxError):
    """Raised when a command tried to write to a read-only mount."""


def _detect_engine() -> str:
    """Return the path to docker or podman, preferring docker.

    Raises :class:`SandboxError` when neither is installed on PATH.
    """
    docker_path = shutil.which("docker")
    if docker_path:
        return docker_path
    podman_path = shutil.which("podman")
    if podman_path:
        return podman_path
    raise SandboxError(
        "No container engine found on PATH. Install docker or podman to use "
        "the codeprobe sandbox."
    )


def _build_run_command(
    engine: str,
    cmd: list[str] | str,
    mounts: dict[str, str],
    *,
    allow_writes: bool,
    image: str,
    workdir: str | None,
    env: dict[str, str] | None,
) -> list[str]:
    """Build the argv for ``<engine> run ...``.

    Exposed as a module-private helper so unit tests can assert the flags
    without spawning a container.
    """
    mode = "rw" if allow_writes else "ro"
    argv: list[str] = [engine, "run", "--rm", "--network=none"]

    if workdir is not None:
        argv += ["-w", workdir]

    if env:
        for key, value in env.items():
            argv += ["-e", f"{key}={value}"]

    for host_path, container_path in mounts.items():
        argv += ["-v", f"{host_path}:{container_path}:{mode}"]

    argv.append(image)

    if isinstance(cmd, str):
        # Wrap string commands in `sh -c` so shell features (pipes,
        # redirection, globbing) work as the caller expects.
        argv += ["sh", "-c", cmd]
    else:
        argv += list(cmd)

    return argv


def _looks_like_ro_write_failure(stderr: str) -> bool:
    """Return True when stderr looks like a write-to-ro-mount error."""
    haystack = stderr.lower()
    return any(needle in haystack for needle in _RO_WRITE_STDERR_PATTERNS)


def run_in_sandbox(
    cmd: list[str] | str,
    mounts: dict[str, str],
    *,
    allow_writes: bool = False,
    image: str = DEFAULT_IMAGE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    workdir: str | None = None,
    env: dict[str, str] | None = None,
) -> SandboxResult:
    """Run ``cmd`` inside a sandbox container and capture its output.

    Parameters
    ----------
    cmd:
        Either a shell string (wrapped in ``sh -c``) or a list of argv tokens
        passed straight to the container entrypoint.
    mounts:
        Mapping of host path to container path. Host paths must already
        exist. When ``allow_writes`` is False (the default) every mount is
        bound ``:ro``.
    allow_writes:
        When True, mounts are bound ``:rw`` — required when the caller
        actually wants the sandbox to mutate the worktree.
    image:
        Container image tag. Defaults to ``codeprobe-sandbox:sg-only``
        (built from ``src/codeprobe/sandbox/Dockerfile.sg_only``).
    timeout:
        Wall-clock timeout in seconds. Exceeding it raises
        :class:`SandboxError`.
    workdir:
        Optional ``-w`` working directory inside the container.
    env:
        Optional environment variables forwarded with ``-e KEY=VAL``.

    Returns
    -------
    :class:`SandboxResult`

    Raises
    ------
    SandboxError
        No container engine on PATH, subprocess timeout, or unexpected OS
        error while launching the engine.
    SandboxWriteDenied
        Command exited non-zero with stderr indicating a write to a
        read-only mount.
    """
    engine = _detect_engine()
    argv = _build_run_command(
        engine,
        cmd,
        mounts,
        allow_writes=allow_writes,
        image=image,
        workdir=workdir,
        env=env,
    )

    logger.debug("sandbox run: %s", argv)

    start = time.perf_counter()
    try:
        completed = subprocess.run(  # noqa: S603 — argv list, no shell=True
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(
            f"sandbox command timed out after {timeout:.1f}s: {argv!r}"
        ) from exc
    except FileNotFoundError as exc:
        # Defensive — _detect_engine already checked, but the engine binary
        # could be removed between that call and subprocess.run.
        raise SandboxError(f"sandbox engine not executable: {engine}") from exc

    duration_ms = int((time.perf_counter() - start) * 1000)

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    exit_code = completed.returncode

    if (
        exit_code != 0
        and not allow_writes
        and _looks_like_ro_write_failure(stderr)
    ):
        raise SandboxWriteDenied(
            f"sandbox blocked write to read-only mount (exit {exit_code}): "
            f"{stderr.strip()}"
        )

    return SandboxResult(
        stdout=stdout,
        stderr=stderr,
        exit_code=exit_code,
        duration_ms=duration_ms,
    )
