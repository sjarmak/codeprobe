"""Write mined tasks to the experiment directory structure."""

from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import asdict
from pathlib import Path

from codeprobe.models.task import Task

logger = logging.getLogger(__name__)

_MAX_FALLBACK_LEN = 500
_WHAT_THIS_PR_PATTERN = re.compile(
    r"^#+\s*what\s+this\s+pr\s+does", re.IGNORECASE | re.MULTILINE
)


def _extract_first_paragraph(description: str) -> str:
    """Extract the first paragraph of a description for fallback instructions.

    Strips the title line (first line), takes text up to the first blank line,
    excludes "What this PR does" sections, and truncates to 500 chars.
    """
    lines = description.strip().splitlines()
    # Skip the title (first line) — it's already used as the heading
    body_lines = lines[1:] if len(lines) > 1 else lines
    text = "\n".join(body_lines).strip()

    # Remove "What this PR does" sections (they contain the solution)
    match = _WHAT_THIS_PR_PATTERN.search(text)
    if match:
        text = text[: match.start()].strip()

    # Take only up to the first blank line (first paragraph)
    paragraphs = text.split("\n\n")
    first = paragraphs[0].strip() if paragraphs else ""

    if len(first) > _MAX_FALLBACK_LEN:
        first = first[:_MAX_FALLBACK_LEN] + "..."
    return first


_ALLOWED_COMMAND_PREFIXES = (
    "bash tests/test.sh",
    "pytest ",
    "go test ",
    "npm test ",
)


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

    # Write instruction.md — issue-based when available, fallback otherwise
    repo_name = repo_path.name
    language = task.metadata.language or "unknown"

    if task.metadata.issue_title:
        issue_body = task.metadata.issue_body
        # Strip solution-leaking sections from issue body
        pr_match = _WHAT_THIS_PR_PATTERN.search(issue_body)
        if pr_match:
            issue_body = issue_body[: pr_match.start()].strip()
        instruction = (
            f"# {task.metadata.issue_title}\n\n"
            f"**Repository:** {repo_name}\n"
            f"**Language:** {language}\n\n"
            "## Problem\n\n"
            f"{issue_body}\n\n"
            "## Task Contract\n\n"
            f"- `TASK_REPO_ROOT={repo_path}`\n\n"
            "## Task\n\n"
            "Implement the fix or feature described above. "
            "The test script will verify correctness.\n"
        )
    else:
        # Fallback: use PR title + first paragraph only (not full body)
        pr_hint = _extract_first_paragraph(task.metadata.description)
        instruction = (
            f"# {task.metadata.name}\n\n"
            f"**Repository:** {repo_name}\n"
            f"**Language:** {language}\n\n"
            "## Task\n\n"
            f"{pr_hint}\n\n"
            "## Task Contract\n\n"
            f"- `TASK_REPO_ROOT={repo_path}`\n\n"
            "Implement the changes described above. "
            "The test script will verify correctness.\n"
        )
    instruction_path = task_dir / "instruction.md"
    instruction_path.write_text(instruction, encoding="utf-8")

    # Write tests/test.sh — validate command against allowlist prefixes
    cmd = task.verification.command
    if not any(
        cmd == prefix or cmd.startswith(prefix) for prefix in _ALLOWED_COMMAND_PREFIXES
    ):
        raise ValueError(f"Verification command not in allowlist: {cmd!r}")
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
