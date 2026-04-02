"""Sandbox detection — gates dangerous permission modes to containerized environments."""

from __future__ import annotations

import os
from pathlib import Path


def is_sandboxed() -> bool:
    """Return True if running inside a container (Docker/containerd).

    Checks three signals:
    1. ``/.dockerenv`` file exists (Docker creates this in every container)
    2. ``CODEPROBE_SANDBOX=1`` environment variable is set (explicit opt-in)
    3. ``/proc/1/cgroup`` contains 'docker' or 'containerd'

    Returns False on a bare Linux host without any of the above.
    """
    # Signal 1: /.dockerenv marker file
    if Path("/.dockerenv").exists():
        return True

    # Signal 2: explicit env var
    if os.environ.get("CODEPROBE_SANDBOX") == "1":
        return True

    # Signal 3: cgroup inspection
    try:
        cgroup_content = Path("/proc/1/cgroup").read_text()
        if "docker" in cgroup_content or "containerd" in cgroup_content:
            return True
    except (FileNotFoundError, PermissionError, OSError):
        pass

    return False
