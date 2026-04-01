"""Create and validate eval task directories.

Generates the standard task directory layout::

    <task_id>/
        instruction.md
        task.toml
        tests/
            test.sh
"""

from __future__ import annotations

import logging
import stat
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TaskSpec:
    """Specification for a new eval task."""

    task_id: str
    repo: str
    difficulty: str = "medium"
    category: str = "sdlc"
    description: str = ""
    time_limit_sec: int = 300
    instruction: str = ""
    reward_type: str = "binary"
    tags: tuple[str, ...] = ()
    estimated_duration_sec: int = 300
    resource_tier: str = "medium"


@dataclass(frozen=True)
class ValidationError:
    """A single validation issue found in a task directory."""

    message: str
    severity: str = "error"


def write_task_dir(spec: TaskSpec, base_dir: Path) -> Path:
    """Write a task directory from a TaskSpec.

    Creates the standard layout under ``base_dir/<task_id>/``.

    Returns the task directory path.

    Raises:
        ValueError: If task_id is empty or contains path separators.
    """
    safe_id = Path(spec.task_id).name
    if not safe_id or safe_id != spec.task_id:
        raise ValueError(f"Invalid task id for filesystem use: {spec.task_id!r}")

    task_dir = base_dir / safe_id
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    # instruction.md
    instruction_content = (
        spec.instruction
        if spec.instruction
        else (f"# {spec.task_id}\n\n{spec.description}\n")
    )
    (task_dir / "instruction.md").write_text(
        instruction_content + "\n", encoding="utf-8"
    )

    # task.toml
    toml_content = _generate_task_toml(spec)
    (task_dir / "task.toml").write_text(toml_content, encoding="utf-8")

    # tests/test.sh
    test_sh_content = _generate_test_sh(spec.task_id)
    test_sh_path = tests_dir / "test.sh"
    test_sh_path.write_text(test_sh_content, encoding="utf-8")
    test_sh_path.chmod(0o755)

    logger.info("Scaffolded task %s → %s", spec.task_id, task_dir)
    return task_dir


def validate_task_dir(task_dir: Path) -> list[ValidationError]:
    """Validate that a task directory has the required structure.

    Returns a list of ValidationError objects (empty means valid).
    """
    errors: list[ValidationError] = []

    if not task_dir.exists():
        errors.append(ValidationError(f"Directory does not exist: {task_dir}"))
        return errors

    if not task_dir.is_dir():
        errors.append(ValidationError(f"Not a directory: {task_dir}"))
        return errors

    # Required files
    required_files = [
        ("instruction.md", "instruction.md is missing"),
        ("task.toml", "task.toml is missing"),
        ("tests/test.sh", "tests/test.sh is missing"),
    ]
    for rel_path, msg in required_files:
        file_path = task_dir / rel_path
        if not file_path.is_file():
            errors.append(ValidationError(msg))

    # test.sh must be executable
    test_sh = task_dir / "tests" / "test.sh"
    if test_sh.is_file():
        mode = test_sh.stat().st_mode
        if not (mode & stat.S_IXUSR):
            errors.append(ValidationError("tests/test.sh is not executable"))

    return errors


def _generate_task_toml(spec: TaskSpec) -> str:
    """Generate task.toml content from a TaskSpec."""
    tags_str = ", ".join(f'"{t}"' for t in spec.tags)

    lines = [
        'version = "1.0"',
        "",
        "[metadata]",
        f'name = "{spec.task_id}"',
        f'difficulty = "{spec.difficulty}"',
        f'description = "{_escape_toml(spec.description)}"',
        f'category = "{spec.category}"',
        f"estimated_duration_sec = {spec.estimated_duration_sec}",
        f'resource_tier = "{spec.resource_tier}"',
    ]
    if spec.tags:
        lines.append(f"tags = [{tags_str}]")

    lines.extend(
        [
            "",
            "[task]",
            f'id = "{spec.task_id}"',
            f'repo = "{_escape_toml(spec.repo)}"',
            f"time_limit_sec = {spec.time_limit_sec}",
            "",
            "[verification]",
            'type = "test_script"',
            'command = "bash tests/test.sh"',
            f'reward_type = "{spec.reward_type}"',
            "",
        ]
    )
    return "\n".join(lines)


def _escape_toml(value: str) -> str:
    """Escape special characters for TOML string values."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _generate_test_sh(task_id: str) -> str:
    """Generate a placeholder test.sh script."""
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "\n"
        f"# Verification script for task: {task_id}\n"
        "# TODO: Add test assertions here.\n"
        "#\n"
        "# This script should exit 0 on success, non-zero on failure.\n"
        "# The agent's working directory is the repo root.\n"
        "\n"
        'echo "PASS: placeholder"\n'
        "exit 0\n"
    )
