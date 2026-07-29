"""Wrap an adapter argv for execution inside the agent container (codeprobe-f7rl.5).

When ``resolve_containment`` returns a ``container`` plan, the adapter's
argv is rewritten into a ``<engine> run`` invocation against
the configured agent image. Mount policy:

- identity rw mount of the slot worktree (host path == container path, so
  the prompt's ``TASK_REPO_ROOT`` and the ``-w`` workdir stay valid);
- identity rw mount of the session ``CLAUDE_CONFIG_DIR`` when present
  (per-slot dirs from session isolation carry the auth state);
- identity ro mount of the MCP config tmpfile when present.

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
    env_keys:
        Candidate env var names; a valueless ``-e KEY`` is emitted for
        each one present in *env*.
    image:
        Agent image tag.
    name:
        Container name (``--name``), so a client-side timeout can
        ``<engine> rm -f`` the container.
    env:
        Mapping consulted for *env_keys* presence — the environment the
        engine client will run with. Defaults to ``os.environ``.
    """
    if not cmd:
        raise ValueError("cmd must be a non-empty argv list")
    present = os.environ if env is None else env

    argv: list[str] = [
        engine,
        "run",
        "--rm",
        "--network=bridge",
        "--name",
        name,
        "-v",
        f"{workspace}:{workspace}:rw",
    ]
    if config_dir is not None:
        argv += ["-v", f"{config_dir}:{config_dir}:rw"]
    if mcp_tmpfile is not None:
        argv += ["-v", f"{mcp_tmpfile}:{mcp_tmpfile}:ro"]
    for key in env_keys:
        if key in present:
            argv += ["-e", key]
    argv += ["-w", str(workspace), image, os.path.basename(cmd[0]), *cmd[1:]]
    return argv
