"""Container-based sandbox for agent mutating tools (INV4).

Public API:

- :func:`run_in_sandbox` — shell out to docker (prefer) or podman to run a
  command inside a container with host paths bind-mounted. Mounts default to
  read-only so agent Write/Bash/Edit calls cannot mutate the host worktree.
- :class:`SandboxResult` — stdout/stderr/exit_code/duration_ms envelope.
- :class:`SandboxError` — base runtime failure (missing engine, timeout, etc.).
- :class:`SandboxWriteDeniedError` — raised when the container tried to write to a
  read-only mount.
"""

from __future__ import annotations

from codeprobe.sandbox.runner import (
    SandboxError,
    SandboxResult,
    SandboxWriteDeniedError,
    run_in_sandbox,
)


def __getattr__(name: str) -> object:
    """Re-export shim for ``SandboxWriteDenied`` → ``SandboxWriteDeniedError`` (N818)."""
    if name == "SandboxWriteDenied":
        from codeprobe.sandbox.runner import __getattr__ as _runner_getattr

        return _runner_getattr(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "SandboxError",
    "SandboxResult",
    "SandboxWriteDeniedError",
    "run_in_sandbox",
]
