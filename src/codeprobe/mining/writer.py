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

# Pattern for stripping backtick-wrapped code patterns from instructions
_BACKTICK_PATTERN = re.compile(r"`[^`]+`")
# Pattern for "containing matches for the patterns X, Y, Z" and similar phrases
_PATTERNS_PHRASE = re.compile(
    r"\s*(?:containing\s+matches\s+)?(?:matching|for)\s+the\s+patterns?\s+"
    r"(?:`[^`]+`(?:,\s*)?)+\.?",
    re.IGNORECASE,
)

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

# ---------------------------------------------------------------------------
# Self-contained oracle scorer (vendored into each task's tests/oracle.py)
# No codeprobe install required — only stdlib imports.
# ---------------------------------------------------------------------------
_ORACLE_PY = '''\
#!/usr/bin/env python3
"""Self-contained F1 oracle scorer for org-scale tasks.

Usage: python3 oracle.py <task_dir>

Reads answer.txt and ground_truth.json from task_dir, computes F1,
writes reward.txt, and exits 0 on success (any score) or 1 on error.
"""
import json, sys
from pathlib import Path

def normalize(p):
    p = p.replace("\\\\", "/").strip()
    for pfx in ("./", "/workspace/", "/tmp/", "/app/"):
        while p.startswith(pfx):
            p = p[len(pfx):]
    return p.lstrip("/")

def main():
    task_dir = Path(sys.argv[1])
    gt = json.loads((task_dir / "ground_truth.json").read_text())
    expected = frozenset(normalize(p) for p in gt.get("expected", []) if p)
    if not expected:
        print("FAIL: empty ground truth")
        sys.exit(1)

    answer_file = task_dir / "answer.txt"
    if not answer_file.exists():
        print("FAIL: no answer.txt")
        (task_dir / "reward.txt").write_text("0.0\\n")
        sys.exit(0)

    lines = answer_file.read_text().splitlines()
    agent = frozenset(normalize(l) for l in lines if l.strip() and not l.startswith("#"))
    if not agent:
        print("FAIL: empty answer")
        (task_dir / "reward.txt").write_text("0.0\\n")
        sys.exit(0)

    intersection = len(expected & agent)
    precision = intersection / len(agent)
    recall = intersection / len(expected)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    (task_dir / "reward.txt").write_text(f"{f1:.4f}\\n")
    print(f"score={f1:.4f} precision={precision:.4f} recall={recall:.4f} "
          f"matched={intersection}/{len(expected)} agent_files={len(agent)}")

if __name__ == "__main__":
    main()
'''


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
    "codeprobe oracle-check ",
)


def write_task_dir(
    task: Task,
    base_dir: Path,
    repo_path: Path,
    *,
    curation_backends: tuple[str, ...] = (),
) -> Path:
    """Write a mined task to the experiment directory structure.

    Creates::

        base_dir/task.id/
            instruction.md
            tests/test.sh
            metadata.json

    Returns the task directory path.

    Raises ValueError if task.id contains path separators or is empty.

    When *curation_backends* is provided, the ground_truth.json curation
    provenance block records the backend names that contributed to the
    curated file set.
    """
    # Validate task.id is safe for filesystem use (no path traversal)
    safe_id = Path(task.id).name
    if not safe_id or safe_id != task.id:
        raise ValueError(f"Invalid task id for filesystem use: {task.id!r}")

    task_dir = base_dir / safe_id
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    # Write instruction.md and verification files
    repo_name = repo_path.name
    language = task.metadata.language or "unknown"

    # Oracle tasks get a different output structure
    if task.verification.type == "oracle":
        _write_oracle_task(
            task,
            task_dir,
            tests_dir,
            repo_path,
            safe_id,
            curation_backends=curation_backends,
        )
        return task_dir

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


def _get_family_description(category: str) -> str:
    """Look up the human-readable description for a task family by name."""
    try:
        from codeprobe.mining.org_scale_families import FAMILIES

        for family in FAMILIES:
            if family.name == category:
                return family.description
    except ImportError:
        pass
    return ""


def _strip_location_hints(question: str, family_description: str = "") -> str:
    r"""Remove grep-pattern hints from a question to create a discovery variant.

    Strips backtick-wrapped patterns (e.g. ``\`@Deprecated\```) and
    "matching the patterns ..." phrases so the agent must find the relevant
    code without being told which regex to use.

    When *family_description* is provided, uses it as the replacement clause
    (e.g. "containing deprecated API annotations or markers") so the discovery
    variant still communicates what to look for, just not the exact regex.
    """
    if family_description:
        # Strip leading "Find files" variants since the question already has
        # "find all files" — we only need the qualifying clause
        desc = re.sub(
            r"^find\s+(?:files|all\s+files)\s+",
            "",
            family_description,
            flags=re.IGNORECASE,
        )
        # Lowercase first char for mid-sentence insertion
        desc = desc[0].lower() + desc[1:] if desc else desc
        # Strip trailing period — we add our own
        desc = desc.rstrip(".")
        replacement = f" that {desc}." if desc else " that are relevant to this task."
    else:
        replacement = " that are relevant to this task."
    # Remove "containing matches for the patterns `X`, `Y`, `Z`" phrases,
    # replacing with a description-aware clause
    result = _PATTERNS_PHRASE.sub(replacement, question)
    # Remove remaining backtick-wrapped patterns
    result = _BACKTICK_PATTERN.sub("the relevant patterns", result)
    # Collapse multiple "the relevant patterns" into one
    result = re.sub(
        r"(the relevant patterns(?:,\s*)?){2,}",
        "the relevant patterns",
        result,
    )
    # Clean up whitespace
    result = re.sub(r"  +", " ", result).strip()
    return result


def _write_oracle_task(
    task: Task,
    task_dir: Path,
    tests_dir: Path,
    repo_path: Path,
    safe_id: str,
    *,
    curation_backends: tuple[str, ...] = (),
) -> None:
    """Write an oracle-verified org-scale task.

    Produces::

        task_dir/
            instruction.md       (the question)
            ground_truth.json    (expected answer + commit SHA)
            tests/test.sh        (calls oracle-check, writes reward.txt)
            metadata.json
    """
    repo_name = repo_path.name
    language = task.metadata.language or "unknown"
    question = task.metadata.issue_body or task.metadata.description

    # instruction.md — discovery question: no hints about specific symbols,
    # file paths, or regex patterns.  The agent must discover everything.
    family_desc = _get_family_description(task.metadata.category)
    discovery_question = _strip_location_hints(question, family_desc)
    # Generic heading — never leaks symbol names or file paths
    discovery_title = f"Find {task.metadata.category} patterns in {repo_name}"

    def _build_instruction(q: str, extra_sections: str = "") -> str:
        return (
            f"# {discovery_title}\n\n"
            f"**Repository:** {repo_name}\n"
            f"**Language:** {language}\n\n"
            "## Question\n\n"
            f"{q}\n\n"
            f"{extra_sections}"
            "## Answer Format\n\n"
            "Write your answer to `answer.txt` in the repository root, "
            "listing one file path per line. Do not include explanations "
            "in the file — only file paths.\n\n"
            "## Task Contract\n\n"
            f"- `TASK_REPO_ROOT={repo_path}`\n"
        )

    (task_dir / "instruction.md").write_text(
        _build_instruction(discovery_question), encoding="utf-8"
    )

    # ground_truth.json — oracle answer + commit + pattern provenance
    has_curation = bool(task.verification.oracle_tiers)
    ground_truth: dict[str, object] = {
        "schema_version": 2 if has_curation else 1,
        "oracle_type": task.verification.oracle_type,
        "expected": list(task.verification.oracle_answer),
        "commit": task.metadata.ground_truth_commit,
        "pattern_used": task.metadata.category,
    }
    if task.metadata.ground_truth_commits:
        ground_truth["commits"] = {
            repo_name: sha for repo_name, sha in task.metadata.ground_truth_commits
        }
    if has_curation:
        ground_truth["oracle_tiers"] = dict(task.verification.oracle_tiers)
        # Curation provenance summary (Risk 7: summary only, no raw data)
        ground_truth["curation"] = {
            "backends_used": (
                list(curation_backends) if curation_backends else ["curated"]
            ),
            "file_count": len(task.verification.oracle_tiers),
        }
    (task_dir / "ground_truth.json").write_text(
        json.dumps(ground_truth, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # tests/oracle.py — self-contained F1 scorer (no codeprobe dependency)
    (tests_dir / "oracle.py").write_text(_ORACLE_PY, encoding="utf-8")

    # tests/test.sh — calls oracle.py, writes reward.txt
    test_script = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f"# Oracle verification for org-scale task {safe_id}\n"
        'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
        'TASK_DIR="$(dirname "$SCRIPT_DIR")"\n\n'
        "# Fallback: if agent wrote to stdout instead of answer.txt, use $AGENT_OUTPUT\n"
        'if [ ! -f "$TASK_DIR/answer.txt" ] && [ -n "${AGENT_OUTPUT:-}" ] && [ -f "$AGENT_OUTPUT" ]; then\n'
        '    cp "$AGENT_OUTPUT" "$TASK_DIR/answer.txt"\n'
        "fi\n\n"
        "# Self-contained oracle check — no codeprobe install required\n"
        'python3 "$SCRIPT_DIR/oracle.py" "$TASK_DIR"\n'
    )
    test_sh_path = tests_dir / "test.sh"
    test_sh_path.write_text(test_script, encoding="utf-8")
    test_sh_path.chmod(0o755)

    # metadata.json
    (task_dir / "metadata.json").write_text(
        json.dumps(asdict(task), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
