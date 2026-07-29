"""Wrap an adapter argv for execution inside the agent container (codeprobe-f7rl.5).

When ``resolve_containment`` returns a ``container`` plan, the adapter's
argv is rewritten into a ``<engine> run`` invocation against
the configured agent image. Mount policy:

- identity rw mount of the slot worktree (host path == container path, so
  the prompt's ``TASK_REPO_ROOT`` and the ``-w`` workdir stay valid);
- identity rw mount of the session ``CLAUDE_CONFIG_DIR`` when present
  (per-slot dirs from session isolation carry the auth state);
- identity ro mount of the MCP config tmpfile when present.
- read-only mounts for validated host CA paths rewritten to container-only
  destinations.

Network is ``bridge``: unlike mined-script scoring containers, the agent
must reach the model API. Secrets are forwarded with valueless ``-e KEY``
flags so they never appear in the argv; the engine copies each value from
its own (already whitelist-filtered) client environment.

Pure argv plumbing — mechanical transforms only, no semantic judgments
about the command (ZFC-allowed).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from codeprobe.sandbox.oci_references import validate_runtime_image_reference

_CONTAINER_TMPFS: Final[str] = "/tmp:rw,nosuid,nodev,size=128m,mode=1777"
_CONTAINER_CPUS: Final[str] = "2"
_CONTAINER_MEMORY: Final[str] = "4g"


def containerize_argv(
    cmd: list[str],
    *,
    engine: str,
    workspace: Path,
    config_dir: Path | None,
    mcp_tmpfile: str | None,
    env_keys: list[str],
    image: str,
    name: str,
    env: Mapping[str, str] | None = None,
    readonly_mounts: list[tuple[Path, Path]] | None = None,
    env_values: Mapping[str, str] | None = None,
) -> list[str]:
    """Return *cmd* wrapped in a ``<engine> run`` argv.

    Parameters
    ----------
    cmd:
        The adapter argv. ``cmd[0]`` is a host binary path that does not
        exist inside the image; it is replaced by its basename so the
        image resolves the same CLI on its own PATH. The rest of the argv
        is passed through unchanged.
    engine:
        Container engine binary (docker or podman path).
    workspace:
        Slot worktree — identity-mounted rw and used as ``-w`` workdir.
    config_dir:
        Session ``CLAUDE_CONFIG_DIR`` to identity-mount rw, or ``None``.
    mcp_tmpfile:
        MCP config tmpfile to identity-mount ro, or ``None``.
    readonly_mounts:
        Additional ``(host, container)`` paths to mount read-only.
    env_keys:
        Candidate env var names; a valueless ``-e KEY`` is emitted for
        each one present in *env*.
    env_values:
        Explicit container environment assignments emitted as ``-e KEY=value``.
    image:
        Verified immutable agent image ID.
    name:
        Container name (``--name``), so a client-side timeout can
        ``<engine> rm -f`` the container.
    env:
        Mapping consulted for *env_keys* presence — the environment the
        engine client will run with. Defaults to ``os.environ``.
    """
    if not cmd:
        raise ValueError("cmd must be a non-empty argv list")
    validate_runtime_image_reference("agent container image", image)
    present = os.environ if env is None else env
    argv = _base_run_args(engine)
    argv += ["--name", name]
    argv += _mount_args(workspace, config_dir, mcp_tmpfile, readonly_mounts)
    argv += _env_key_args(env_keys, present)
    argv += _env_value_args(env_values or {})
    argv += ["-w", str(workspace), image]
    argv += [os.path.basename(cmd[0]), *cmd[1:]]
    return argv


def _base_run_args(engine: str) -> list[str]:
    argv = [
        engine,
        "run",
        "--rm",
        "--pull=never",
        "--network=bridge",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        f"--cpus={_CONTAINER_CPUS}",
        f"--memory={_CONTAINER_MEMORY}",
        f"--memory-swap={_CONTAINER_MEMORY}",
        "--pids-limit=256",
        "--read-only",
        "--tmpfs",
        _CONTAINER_TMPFS,
        "-e",
        "HOME=/tmp",
        "-e",
        "TMPDIR=/tmp",
    ]
    if hasattr(os, "getuid") and hasattr(os, "getgid"):
        argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
    return argv


def _mount_args(
    workspace: Path,
    config_dir: Path | None,
    mcp_tmpfile: str | None,
    readonly_mounts: list[tuple[Path, Path]] | None,
) -> list[str]:
    args = ["-v", f"{workspace}:{workspace}:rw"]
    if config_dir is not None:
        args += ["-v", f"{config_dir}:{config_dir}:rw"]
    if mcp_tmpfile is not None:
        args += ["-v", f"{mcp_tmpfile}:{mcp_tmpfile}:ro"]
    for host_path, container_path in readonly_mounts or []:
        args += ["-v", f"{host_path}:{container_path}:ro"]
    return args


def _env_key_args(env_keys: list[str], present: Mapping[str, str]) -> list[str]:
    args: list[str] = []
    for key in env_keys:
        if key in present:
            args += ["-e", key]
    return args


def _env_value_args(env_values: Mapping[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in env_values.items():
        args += ["-e", f"{key}={value}"]
    return args
