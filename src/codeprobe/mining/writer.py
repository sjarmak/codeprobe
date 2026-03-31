"""Write mined tasks to the experiment directory structure."""

from __future__ import annotations

import json
import logging
import shlex
from dataclasses import asdict
from pathlib import Path

from codeprobe.models.task import Task

logger = logging.getLogger(__name__)

_ALLOWED_COMMANDS = frozenset({"bash tests/test.sh"})


def write_task_dir(task: Task, base_dir: Path, repo_path: Path) -> Path:
    """Write a mined task to the experiment directory structure.

    Creates::

        base_dir/task.id/
            instruction.md
            tests/test.sh
            metadata.json

    Returns the task directory path.

    Raises ValueError if task.id contains path separators or is empty.
    """
    # Validate task.id is safe for filesystem use (no path traversal)
    safe_id = Path(task.id).name
    if not safe_id or safe_id != task.id:
        raise ValueError(f"Invalid task id for filesystem use: {task.id!r}")

    task_dir = base_dir / safe_id
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    # Write instruction.md
    instruction = (
        f"# {task.metadata.name}\n\n"
        f"{task.metadata.description}\n\n"
        "Reproduce the changes from this merged PR. "
        "The test script will verify correctness.\n"
    )
    instruction_path = task_dir / "instruction.md"
    instruction_path.write_text(instruction, encoding="utf-8")

    # Write tests/test.sh — use shlex.quote to prevent shell injection
    if task.verification.command not in _ALLOWED_COMMANDS:
        raise ValueError(
            f"Verification command not in allowlist: {task.verification.command!r}"
        )
    test_script = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f"# Verification script for task {safe_id}\n"
        f"# Run tests relevant to merge commit {safe_id}\n"
        f"cd {shlex.quote(str(repo_path))}\n"
        f"{task.verification.command}\n"
    )
    test_sh_path = tests_dir / "test.sh"
    test_sh_path.write_text(test_script, encoding="utf-8")
    test_sh_path.chmod(0o755)

    # Write metadata.json
    metadata_path = task_dir / "metadata.json"
    metadata_path.write_text(
        json.dumps(asdict(task), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    logger.info("Wrote task %s → %s", task.id, task_dir)
    return task_dir
