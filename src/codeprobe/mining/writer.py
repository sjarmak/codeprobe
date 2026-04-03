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
_MAX_ISSUE_BODY_LEN = 1500
_WHAT_THIS_PR_PATTERN = re.compile(
    r"^#+\s*what\s+this\s+pr\s+does[^\n]*", re.IGNORECASE | re.MULTILINE
)

# Common PR template section headers that contain noise or solution details.
# Each pattern matches the entire heading line (via [^\n]*) so no trailing
# fragments are left behind.
_PR_TEMPLATE_SECTIONS = re.compile(
    r"^#+\s*(?:"
    r"what\s+type\s+of\s+pr\s+is\s+this"
    r"|which\s+issue"
    r"|special\s+notes?\s+for"
    r"|does\s+this\s+pr\s+introduce"
    r"|additional\s+documentation"
    r"|release[\s-]*note"
    r"|checklist"
    r"|how\s+has\s+this\s+been\s+tested"
    r"|screenshots?"
    r"|related\s+(?:issues?|prs?|pull\s+requests?)"
    r"|testing\s+done"
    r")[^\n]*",
    re.IGNORECASE | re.MULTILINE,
)

# Lines that are just PR template labels (e.g., "/kind feature", "/area foo")
_PR_TEMPLATE_LABEL = re.compile(r"^/\w+\s+\S+", re.MULTILINE)

# HTML comments (including multiline ones like PR template instructions)
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)

# Fenced code blocks with specific info strings that are noise (release notes, docs)
_NOISE_FENCED_BLOCKS = re.compile(r"```(?:release-note|docs)\b.*?```", re.DOTALL)

# <details> blocks (often contain verbose reproduction steps, version dumps)
_DETAILS_BLOCK = re.compile(r"<details>.*?</details>", re.DOTALL)


def _strip_pr_template(text: str) -> str:
    """Remove common PR template sections and label lines from text.

    Preserves content that precedes any template section and the
    "What this PR does / why we need it" section body (the actual
    problem description), while stripping the heading itself.
    """
    # Normalise Windows line endings for consistent splitting
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Strip HTML comments (PR template instructions like <!-- Tips for you... -->)
    text = _HTML_COMMENT.sub("", text)

    # Strip noise fenced blocks (```release-note ... ```, ```docs ... ```)
    text = _NOISE_FENCED_BLOCKS.sub("", text)

    # Extract "What this PR does" section content — this is the useful part
    what_match = _WHAT_THIS_PR_PATTERN.search(text)
    what_body = ""
    if what_match:
        # Find the content between this heading and the next heading
        after = text[what_match.end() :]
        next_heading = re.search(r"^#+\s", after, re.MULTILINE)
        section = after[: next_heading.start()] if next_heading else after
        what_body = section.strip()

    # Remove all template sections (including "What this PR does" heading)
    cleaned = _PR_TEMPLATE_SECTIONS.sub("", text)
    cleaned = _WHAT_THIS_PR_PATTERN.sub("", cleaned)

    # Remove label lines ("/kind feature", "/area scheduling")
    cleaned = _PR_TEMPLATE_LABEL.sub("", cleaned)

    # Collapse runs of blank lines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

    # If we extracted a "What this PR does" body, prepend it if it's not
    # already present (it got removed with its heading)
    if what_body and what_body not in cleaned:
        cleaned = what_body + ("\n\n" + cleaned if cleaned else "")

    return cleaned


def _extract_first_paragraph(description: str) -> str:
    """Extract the meaningful first paragraph from a PR description.

    Strips the title line (first line), removes common PR template sections
    (kubernetes-style "What type of PR is this?", etc.), and returns the
    first non-empty paragraph, truncated to 500 chars.
    """
    lines = description.strip().splitlines()
    # Skip the title (first line) — it's already used as the heading
    body_lines = lines[1:] if len(lines) > 1 else lines
    text = "\n".join(body_lines).strip()

    # Strip PR template boilerplate
    text = _strip_pr_template(text)

    # Take only up to the first blank line (first paragraph)
    paragraphs = text.split("\n\n")
    first = ""
    for p in paragraphs:
        candidate = p.strip()
        if candidate:
            first = candidate
            break

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

        # Only apply regex cleanup for non-LLM content — LLM output is clean
        if task.metadata.enrichment_source != "llm":
            # Strip solution-leaking sections from issue body
            pr_match = _WHAT_THIS_PR_PATTERN.search(issue_body)
            if pr_match:
                issue_body = issue_body[: pr_match.start()].strip()
            # Strip HTML comments, <details> blocks, and noise fenced blocks
            issue_body = _HTML_COMMENT.sub("", issue_body)
            issue_body = _DETAILS_BLOCK.sub("", issue_body)
            issue_body = _NOISE_FENCED_BLOCKS.sub("", issue_body)
            # Collapse blank lines and truncate to keep instructions focused
            issue_body = re.sub(r"\n{3,}", "\n\n", issue_body).strip()
            if len(issue_body) > _MAX_ISSUE_BODY_LEN:
                issue_body = issue_body[:_MAX_ISSUE_BODY_LEN] + "\n\n[...truncated]"
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
