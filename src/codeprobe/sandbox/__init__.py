"""Container-based sandbox for agent mutating tools (INV4).

Public API:

- :func:`run_in_sandbox` — shell out to docker (prefer) or podman to run a
  command inside a container with host paths bind-mounted. Mounts default to
  read-only so agent Write/Bash/Edit calls cannot mutate the host worktree.
- :class:`SandboxResult` — stdout/stderr/exit_code/duration_ms envelope.
- :class:`SandboxError` — base runtime failure (missing engine, timeout, etc.).
- :class:`SandboxWriteDenied` — raised when the container tried to write to a
  read-only mount.
"""

from __future__ import annotations

from codeprobe.sandbox.runner import (
    SandboxError,
    SandboxResult,
    SandboxWriteDenied,
    run_in_sandbox,
)

__all__ = [
    "SandboxError",
    "SandboxResult",
    "SandboxWriteDenied",
    "run_in_sandbox",
]
