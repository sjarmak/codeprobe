"""Write mined tasks to the experiment directory structure."""

from __future__ import annotations

import datetime
import json
import logging
import re
import shlex
from dataclasses import asdict, replace
from pathlib import Path, PurePosixPath

from codeprobe.mining.confidence import (
    score_task_confidence,
    write_confidence_file,
)
from codeprobe.mining.extractor import _is_safe_relative_path
from codeprobe.models.task import Task


def _emit_confidence(task_dir: Path) -> None:
    """Score the freshly-written task and persist confidence.json.

    Cross-validation signal is neutral at mining time — it requires a
    cross-validation report which is generated post-mining. Callers can
    re-score later via :func:`mining.confidence.score_tasks_dir`.
    """
    try:
        score = score_task_confidence(task_dir)
        write_confidence_file(score, task_dir)
    except (OSError, ValueError) as exc:
        logger.warning("Failed to write confidence.json for %s: %s", task_dir, exc)

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
writes reward.txt + metrics.json, and exits 0 on success (any score)
or 1 on error.

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

metrics.json shape:
  {"score": float, "metric": "f1"|"weighted_f1",
   "f1": float, "precision": float, "recall": float,
   "matched": int, "expected_count": int, "agent_files_count": int,
   "weighted_recall": float|null}
The host scorer (ContinuousScorer) merges this into scoring_details so
F1 stays the headline number while precision/recall remain inspectable.
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

    def write_metrics(payload):
        (task_dir / "metrics.json").write_text(
            json.dumps(payload, sort_keys=True) + "\\n",
            encoding="utf-8",
        )

    answer_file = task_dir / "answer.txt"
    if not answer_file.exists():
        print("FAIL: no answer.txt")
        (task_dir / "reward.txt").write_text("0.0\\n")
        write_metrics({
            "score": 0.0, "metric": "f1", "f1": 0.0,
            "precision": 0.0, "recall": 0.0,
            "matched": 0, "expected_count": len(expected_set),
            "agent_files_count": 0, "weighted_recall": None,
            "error": "no_answer_file",
        })
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
        write_metrics({
            "score": 0.0, "metric": "f1", "f1": 0.0,
            "precision": 0.0, "recall": 0.0,
            "matched": 0, "expected_count": len(expected_set),
            "agent_files_count": 0, "weighted_recall": None,
            "error": "empty_answer",
        })
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
    write_metrics({
        "score": round(primary, 6),
        "metric": metric,
        "f1": round(f1, 6),
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "matched": intersection,
        "expected_count": len(expected_set),
        "agent_files_count": len(agent_set),
        "weighted_recall": round(weighted_recall, 6) if weighted_recall is not None else None,
    })
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


# ---------------------------------------------------------------------------
# Structured-retrieval oracle (oracle_type='structured_retrieval')
# ---------------------------------------------------------------------------
#
# The structured oracle reads ``answer.json`` (INV1: no $AGENT_OUTPUT
# fallback — malformed or missing answers score 0.0 with an explicit
# ``error`` entry in ``scoring.json``). The schema is four independent
# fields that are scored separately and averaged:
#
#     {
#       "files":   [{"repo": str, "path": str}, ...],
#       "symbols": [{"repo": str, "path": str, "symbol": str}, ...],
#       "chain":   [{"repo": str, "path": str, "symbol": str}, ...],
#       "text":    str,
#     }
#
# Each non-empty-ground-truth field contributes equally to the combined
# score. Missing fields in ``ground_truth.json`` are skipped.
# ---------------------------------------------------------------------------
_ORACLE_STRUCTURED = '''\
#!/usr/bin/env python3
"""Self-contained structured-retrieval oracle.

Usage: python3 oracle.py <task_dir>

Reads ``answer.json`` and ``ground_truth.json`` from <task_dir>, scores
each field (``files``, ``symbols``, ``chain``, ``text``) independently,
combines them with an arithmetic mean across fields that have non-empty
ground truth, writes ``reward.txt`` + ``scoring.json`` into <task_dir>,
and always exits 0 (score represents the verdict).

Missing or malformed ``answer.json`` is scored 0.0 with a populated
``scoring.json["error"]`` — there is NO stdout-capture fallback here by
design (INV1).
"""
import json, sys, re
from pathlib import Path


def _write_scoring(task_dir, score, scoring):
    scoring["score"] = float(score)
    (task_dir / "scoring.json").write_text(
        json.dumps(scoring, indent=2, sort_keys=True) + "\\n",
        encoding="utf-8",
    )
    (task_dir / "reward.txt").write_text(f"{float(score):.4f}\\n", encoding="utf-8")


def _tuple_set(items, keys):
    out = set()
    if not isinstance(items, list):
        return out
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            key = tuple(str(it[k]).strip() for k in keys)
        except KeyError:
            continue
        if all(part for part in key):
            out.add(key)
    return out


def _f1(expected, actual):
    if not expected and not actual:
        return 1.0, {"precision": 1.0, "recall": 1.0, "tp": 0, "fp": 0, "fn": 0}
    if not expected:
        return 0.0, {"precision": 0.0, "recall": 0.0, "tp": 0,
                     "fp": len(actual), "fn": 0}
    if not actual:
        return 0.0, {"precision": 0.0, "recall": 0.0, "tp": 0,
                     "fp": 0, "fn": len(expected)}
    matched = expected & actual
    precision = len(matched) / len(actual)
    recall = len(matched) / len(expected)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return f1, {
        "precision": precision,
        "recall": recall,
        "tp": len(matched),
        "fp": len(actual - expected),
        "fn": len(expected - actual),
    }


_TOKEN_RE = re.compile(r"\\w+")


def _tokenize(text):
    return frozenset(m.group(0).lower() for m in _TOKEN_RE.finditer(text or ""))


def _text_score(expected, actual):
    e = _tokenize(expected)
    a = _tokenize(actual)
    if not e and not a:
        return 1.0, {"jaccard": 1.0, "expected_tokens": 0, "actual_tokens": 0}
    union = e | a
    inter = e & a
    jacc = len(inter) / len(union) if union else 0.0
    return jacc, {
        "jaccard": jacc,
        "expected_tokens": len(e),
        "actual_tokens": len(a),
    }


def main():
    task_dir = Path(sys.argv[1])
    scoring = {"schema": "structured_retrieval.v1", "fields": {}, "error": None}

    answer_path = task_dir / "answer.json"
    if not answer_path.exists():
        scoring["error"] = "missing answer.json"
        _write_scoring(task_dir, 0.0, scoring)
        return

    try:
        answer = json.loads(answer_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        scoring["error"] = f"malformed answer.json: {exc}"
        _write_scoring(task_dir, 0.0, scoring)
        return

    if not isinstance(answer, dict):
        scoring["error"] = "answer.json must be a JSON object"
        _write_scoring(task_dir, 0.0, scoring)
        return

    gt_path = task_dir / "ground_truth.json"
    if not gt_path.exists():
        scoring["error"] = "missing ground_truth.json"
        _write_scoring(task_dir, 0.0, scoring)
        return

    try:
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        scoring["error"] = f"malformed ground_truth.json: {exc}"
        _write_scoring(task_dir, 0.0, scoring)
        return

    # Ground truth may be wrapped under "expected" or live at the top level.
    gt_payload = gt.get("expected", gt) if isinstance(gt, dict) else {}
    if not isinstance(gt_payload, dict):
        scoring["error"] = "ground_truth.json expected payload is not an object"
        _write_scoring(task_dir, 0.0, scoring)
        return

    per_field_scores = []

    # files — (repo, path) tuples
    if "files" in gt_payload:
        exp = _tuple_set(gt_payload.get("files") or [], ("repo", "path"))
        act = _tuple_set(answer.get("files") or [], ("repo", "path"))
        s, detail = _f1(exp, act)
        scoring["fields"]["files"] = {"score": s, **detail,
                                       "expected": len(exp), "actual": len(act)}
        if exp:
            per_field_scores.append(s)

    # symbols — (repo, path, symbol) tuples
    if "symbols" in gt_payload:
        exp = _tuple_set(gt_payload.get("symbols") or [], ("repo", "path", "symbol"))
        act = _tuple_set(answer.get("symbols") or [], ("repo", "path", "symbol"))
        s, detail = _f1(exp, act)
        scoring["fields"]["symbols"] = {"score": s, **detail,
                                         "expected": len(exp), "actual": len(act)}
        if exp:
            per_field_scores.append(s)

    # chain — (repo, path, symbol) tuples, scored as a set like symbols
    if "chain" in gt_payload:
        exp = _tuple_set(gt_payload.get("chain") or [], ("repo", "path", "symbol"))
        act = _tuple_set(answer.get("chain") or [], ("repo", "path", "symbol"))
        s, detail = _f1(exp, act)
        scoring["fields"]["chain"] = {"score": s, **detail,
                                       "expected": len(exp), "actual": len(act)}
        if exp:
            per_field_scores.append(s)

    # text — Jaccard on tokenized word bag
    if "text" in gt_payload:
        exp = gt_payload.get("text") or ""
        act = answer.get("text") or ""
        if not isinstance(exp, str):
            exp = ""
        if not isinstance(act, str):
            act = ""
        s, detail = _text_score(exp, act)
        scoring["fields"]["text"] = {"score": s, **detail}
        if exp.strip():
            per_field_scores.append(s)

    if not per_field_scores:
        # No scored fields — ground truth was entirely empty. Treat as 0.0
        # with an explicit error so the caller notices the malformed GT.
        scoring["error"] = "ground truth had no scorable fields"
        _write_scoring(task_dir, 0.0, scoring)
        return

    combined = sum(per_field_scores) / len(per_field_scores)
    scoring["num_fields_scored"] = len(per_field_scores)
    _write_scoring(task_dir, combined, scoring)
    print(
        f"score={combined:.4f} fields_scored={len(per_field_scores)} "
        f"per_field={per_field_scores}"
    )


if __name__ == "__main__":
    main()
'''


# Section appended to ``instruction.md`` for structured-retrieval tasks.
# Agents write ``answer.json`` in the repo root; the oracle scores each
# field independently.
_STRUCTURED_ANSWER_SCHEMA_SECTION = """\
## Expected answer.json

Write your answer to `answer.json` in the repository root (no other
output channel is scored — stdout/AGENT_OUTPUT is NOT a fallback). The
file must be a single JSON object with any subset of these four fields.
Each field is scored independently and combined into a single score:

- `files` — array of `{"repo": str, "path": str}` objects for files
  relevant to the task.
- `symbols` — array of `{"repo": str, "path": str, "symbol": str}`
  objects naming the specific symbol(s) (functions, classes, etc.) you
  found.
- `chain` — array of `{"repo": str, "path": str, "symbol": str}` objects
  representing the ordered dependency or call chain.
- `text` — string summary of your findings; scored by token overlap.

Example:

```json
{
  "files": [{"repo": "kubernetes", "path": "pkg/scheduler.go"}],
  "symbols": [{"repo": "kubernetes", "path": "pkg/scheduler.go",
                "symbol": "Scheduler.Run"}],
  "chain":  [{"repo": "kubernetes", "path": "pkg/scheduler.go",
                "symbol": "Scheduler.Run"}],
  "text":   "Run is the top-level scheduling loop."
}
```

Missing or malformed `answer.json` scores 0.0 with an explicit error in
`scoring.json`.
"""


def _coerce_structured_expected(oracle_answer: object) -> dict[str, object]:
    """Coerce ``TaskVerification.oracle_answer`` to the structured schema.

    ``oracle_answer`` is typed as ``tuple[str, ...]`` but the structured
    retrieval flow needs a four-field dict. Supported input shapes:

      1. A dict — already the right shape.
      2. A tuple containing a single dict — stored via object-tuple.
      3. A tuple whose first element is a JSON string encoding the dict
         — the canonical storage format given the hashable-tuple
         constraint on the dataclass.

    Returns an empty dict when the shape doesn't match, so the oracle
    reports "no scorable fields" rather than silently passing.
    """
    if isinstance(oracle_answer, dict):
        return dict(oracle_answer)
    if isinstance(oracle_answer, tuple) and oracle_answer:
        first = oracle_answer[0]
        if isinstance(first, dict):
            return dict(first)
        if isinstance(first, str):
            try:
                decoded = json.loads(first)
            except (json.JSONDecodeError, ValueError):
                return {}
            if isinstance(decoded, dict):
                return decoded
    return {}


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


def resolve_checkpoint_scripts(task: Task) -> dict[str, str] | None:
    """Return the verifier scripts for *task*'s checkpoint contract, if any.

    Callers (the mine CLI, test fixtures) can pass the result to
    :func:`write_task_dir` so they don't have to import category-specific
    helpers directly. Returns ``None`` when the task has no checkpoints
    so writer emits nothing extra.
    """
    if not task.verification.checkpoints:
        return None

    category = task.metadata.category or ""
    if category == "change-scope-audit":
        from codeprobe.mining.org_scale import CHANGE_SCOPE_CHECKPOINT_SCRIPTS

        return dict(CHANGE_SCOPE_CHECKPOINT_SCRIPTS)

    if category == "architecture_comprehension" or task.metadata.task_type == (
        "architecture_comprehension"
    ):
        from codeprobe.mining.comprehension import COMPREHENSION_CHECKPOINT_SCRIPTS

        return dict(COMPREHENSION_CHECKPOINT_SCRIPTS)

    return None


def _write_checkpoints(
    task: Task,
    tests_dir: Path,
    checkpoint_scripts: dict[str, str] | None,
) -> None:
    """Emit per-checkpoint verifier scripts + ``tests/checkpoints.json``.

    No-op when ``task.verification.checkpoints`` is empty — this is the
    gating contract that keeps single-step tasks (plain sdlc_code_change,
    non-multi-step org_scale, etc.) from emitting ``tests/verifiers/`` or
    ``tests/checkpoints.json`` (R17 acceptance #4).

    ``checkpoint_scripts`` maps the verifier filename (e.g.
    ``step1_answer_provided.sh``) to its bash body. Missing entries get
    a safe stub that exits 0, keeping writer behavior total even when a
    caller forgets to provide a body.
    """
    checkpoints = task.verification.checkpoints
    if not checkpoints:
        return

    verifiers_dir = tests_dir / "verifiers"
    verifiers_dir.mkdir(parents=True, exist_ok=True)

    # Resolve built-in scripts from the task's category when the caller
    # didn't pass an explicit map. Keeps the mine CLI a one-liner without
    # every dispatcher having to import category-specific constants.
    scripts = checkpoint_scripts or resolve_checkpoint_scripts(task) or {}
    for cp in checkpoints:
        script_body = scripts.get(
            cp.verifier,
            "#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n",
        )
        verifier_path = verifiers_dir / cp.verifier
        verifier_path.write_text(script_body, encoding="utf-8")
        verifier_path.chmod(0o755)

    checkpoints_payload = [
        {"name": cp.name, "weight": cp.weight, "verifier": cp.verifier}
        for cp in checkpoints
    ]
    (tests_dir / "checkpoints.json").write_text(
        json.dumps(checkpoints_payload, indent=2) + "\n",
        encoding="utf-8",
    )


def write_quarantined_task(
    *,
    task_id: str,
    family: str,
    repo: str,
    symbol: str,
    defining_file: str,
    instruction_title: str,
    instruction_body: str,
    divergence_report: dict,
    base_dir: Path,
) -> Path:
    """Drop a quarantined consensus candidate under *base_dir*.

    The quarantine directory does NOT contain ``ground_truth.json`` or
    ``test.sh`` — by construction the backends could not agree on what the
    answer should be. It contains only the artifacts a reviewer needs to
    triage the candidate:

    - ``divergence_report.json`` — full per-backend file lists and pairwise
      F1; the same schema written for shipped consensus tasks.
    - ``instruction.md`` — the question that *would* have been asked, so
      the reviewer can decide whether to keep the candidate.
    - ``metadata.json`` — task_id, family, repo, symbol, defining_file.

    Returns the directory path. Raises :class:`ValueError` if *task_id* is
    unsafe for filesystem use (matches :func:`write_task_dir`).
    """
    safe_id = Path(task_id).name
    if not safe_id or safe_id != task_id:
        raise ValueError(
            f"Invalid task id for quarantine output: {task_id!r}"
        )

    task_dir = base_dir / safe_id
    task_dir.mkdir(parents=True, exist_ok=True)

    instruction = (
        f"# {instruction_title}\n\n"
        f"**Repository:** {repo}\n"
        f"**Family:** {family}\n"
        f"**Symbol:** `{symbol}` (defined in `{defining_file}`)\n\n"
        "## Question\n\n"
        f"{instruction_body}\n\n"
        "## Quarantine Reason\n\n"
        "This task was quarantined by consensus mining because the "
        "configured backends did not agree above the F1 threshold on "
        "what the ground truth should be. See `divergence_report.json` "
        "for the per-backend file lists and pairwise metrics.\n"
    )
    (task_dir / "instruction.md").write_text(instruction, encoding="utf-8")

    (task_dir / "divergence_report.json").write_text(
        json.dumps(divergence_report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    (task_dir / "metadata.json").write_text(
        json.dumps(
            {
                "task_id": task_id,
                "family": family,
                "repo": repo,
                "symbol": symbol,
                "defining_file": defining_file,
                "status": "quarantined",
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    logger.info("Quarantined %s/%s -> %s", family, symbol, task_dir)
    return task_dir


def write_task_dir(
    task: Task,
    base_dir: Path,
    repo_path: Path,
    *,
    curation_backends: tuple[str, ...] = (),
    ground_truth: dict | None = None,
    checkpoint_scripts: dict[str, str] | None = None,
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
        _write_checkpoints(task, tests_dir, checkpoint_scripts)
        _emit_confidence(task_dir)
        return task_dir

    # Dual-verification tasks: direct test.sh + artifact answer.json
    if task.verification.verification_mode == "dual":
        _write_dual_task(task, task_dir, tests_dir, repo_path, safe_id)
        _write_checkpoints(task, tests_dir, checkpoint_scripts)
        _emit_confidence(task_dir)
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

    # Write instruction_mcp.md variant for MCP / org-scale / SG-enriched tasks.
    # Trigger widened per PRD: task_type in mcp_tool_usage / org_scale_cross_repo,
    # or org_scale=True, or sg_repo set.
    if _mcp_variant_triggered(task):
        _write_mcp_instruction_variant(task, task_dir, instruction)

    # Write tests/test.sh — weighted checklist for sdlc-schema ground truth,
    # otherwise a plain wrapper. Both paths validate the mined command against
    # the allowlist in _build_*_script.
    use_weighted = ground_truth is not None and str(
        ground_truth.get("schema_version", "")
    ).startswith("sdlc-")

    if use_weighted and ground_truth is not None:
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

    # Per-checkpoint verifier scripts + tests/checkpoints.json. Gated
    # behind task.verification.checkpoints (R17 acceptance #4).
    _write_checkpoints(task, tests_dir, checkpoint_scripts)

    _emit_confidence(task_dir)

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

# Task-type values that trigger the instruction_mcp.md variant when the task
# does not already carry an explicit org_scale or sg_repo marker.
_MCP_VARIANT_TASK_TYPES: frozenset[str] = frozenset(
    {"mcp_tool_usage", "org_scale_cross_repo"}
)


def _mcp_variant_triggered(task: Task) -> bool:
    """Return True when this task should receive an ``instruction_mcp.md``.

    Triggers on any of:
      - ``task.metadata.task_type`` in the MCP/org-scale task-type set
      - ``task.metadata.org_scale`` is truthy
      - ``task.metadata.sg_repo`` is a non-empty string

    This widens the original ``task_type == "mcp_tool_usage"`` check so
    org-scale and Sourcegraph-enriched tasks also get an MCP variant
    rendered from the capability registry.
    """
    meta = task.metadata
    if meta.task_type in _MCP_VARIANT_TASK_TYPES:
        return True
    if meta.org_scale:
        return True
    return bool(meta.sg_repo)


def _render_mcp_section() -> str:
    """Render the MCP capability section from the Jinja capability template.

    Resolves the tool surface from :mod:`codeprobe.mcp.capabilities` rather
    than a hand-rolled Sourcegraph-flavored string table, so preambles,
    fixtures, and evaluations share a single source of truth.
    """
    # Deferred import: keeps ``writer`` importable in environments where
    # the preamble renderer's transitive deps (jinja2) are not present,
    # and only pays the import cost for MCP-variant tasks.
    from codeprobe.preambles.templates import render

    return render("mcp_base.md.j2")


def _write_mcp_instruction_variant(
    task: Task,
    task_dir: Path,
    base_instruction: str,
) -> None:
    """Write instruction_mcp.md alongside instruction.md for MCP tasks.

    Appends the capability-rendered MCP section to the base instruction
    so agents with MCP access know which capabilities are available. The
    body is sourced from :mod:`codeprobe.mcp.capabilities` via the
    ``mcp_base.md.j2`` template — NOT from any preamble string table.
    """
    mcp_suite = task.metadata.mcp_suite or "sourcegraph"
    mcp_section = _render_mcp_section()
    mcp_instruction = (
        base_instruction.rstrip()
        + "\n\n"
        + mcp_section.rstrip()
        + f"\n\n**MCP Suite:** {mcp_suite}\n"
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

    # Structured-retrieval tasks ship a four-field answer.json schema and
    # a dedicated vendored oracle. They bypass the "find files matching
    # these patterns" discovery framing used by file_list tasks.
    is_structured = task.verification.oracle_type == "structured_retrieval"

    # MCP-advantaged families embed the symbol name and definition file
    # as essential task information — stripping them makes the task ambiguous.
    # Pattern-based families strip regex hints so the agent must discover them.
    is_mcp_family = task.metadata.category in _MCP_CATEGORIES

    if is_structured:
        discovery_question = question
        discovery_title = task.metadata.issue_title or (
            f"Structured retrieval task — {task.metadata.category or repo_name}"
        )
    elif is_mcp_family:
        discovery_question = question
        discovery_title = task.metadata.issue_title or (
            f"Find {task.metadata.category} patterns in {repo_name}"
        )
    else:
        family_desc = _get_family_description(task.metadata.category)
        discovery_question = _strip_location_hints(question, family_desc)
        discovery_title = f"Find {task.metadata.category} patterns in {repo_name}"

    def _build_instruction(q: str, extra_sections: str = "") -> str:
        if is_structured:
            return (
                f"# {discovery_title}\n\n"
                f"**Repository:** {repo_name}\n"
                f"**Language:** {language}\n\n"
                "## Question\n\n"
                f"{q}\n\n"
                f"{extra_sections}"
                f"{_STRUCTURED_ANSWER_SCHEMA_SECTION}\n"
                "## Task Contract\n\n"
                f"- `TASK_REPO_ROOT={repo_path}`\n"
            )
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

    base_instruction = _build_instruction(discovery_question)
    (task_dir / "instruction.md").write_text(base_instruction, encoding="utf-8")

    # Emit the MCP instruction variant for org-scale / SG / MCP tasks so the
    # widened R1 trigger also covers oracle-typed tasks (the main writer path
    # short-circuits for oracle tasks and never reaches the MCP trigger).
    if _mcp_variant_triggered(task):
        _write_mcp_instruction_variant(task, task_dir, base_instruction)

    # ground_truth.json — oracle answer + commit + pattern provenance
    has_curation = bool(task.verification.oracle_tiers)
    if is_structured:
        # Structured retrieval stores the full four-field schema under
        # ``expected`` so the vendored oracle can read it uniformly.
        structured_expected = _coerce_structured_expected(
            task.verification.oracle_answer
        )
        ground_truth: dict[str, object] = {
            "schema_version": "structured_retrieval.v1",
            "oracle_type": "structured_retrieval",
            "expected": structured_expected,
            "commit": task.metadata.ground_truth_commit,
            "pattern_used": task.metadata.category,
            "repo": task.repo,
        }
    else:
        ground_truth = {
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

    # Oracle-curator backend consensus (codeprobe-zat9). Surfaces which
    # backends contributed to the curated answer key so downstream
    # bias-warning code can detect oracle/tool tautology by comparing
    # against the agent's MCP surface. Empty for tasks not built through
    # the multi-backend consensus path.
    if task.metadata.oracle_backends_consensus:
        ground_truth["oracle_backends_consensus"] = list(
            task.metadata.oracle_backends_consensus
        )
    (task_dir / "ground_truth.json").write_text(
        json.dumps(ground_truth, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    # tests/oracle.py — self-contained scorer. Structured-retrieval tasks
    # get the four-field JSON oracle; legacy file-list tasks get the F1
    # file-list oracle (preserved path per R2 acceptance criterion).
    if is_structured:
        (tests_dir / "oracle.py").write_text(_ORACLE_STRUCTURED, encoding="utf-8")
    else:
        (tests_dir / "oracle.py").write_text(_ORACLE_PY, encoding="utf-8")

    # tests/test.sh — calls oracle.py, writes reward.txt
    if is_structured:
        # INV1: structured_retrieval does NOT fall back to $AGENT_OUTPUT.
        # Missing / malformed answer.json is an honest 0.0 with an error.
        test_script = (
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n\n"
            f"# Structured-retrieval oracle for task {safe_id}\n"
            'SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"\n'
            'TASK_DIR="$(dirname "$SCRIPT_DIR")"\n'
            '_CP_REPO_DEFAULT="$(cd "$TASK_DIR/.." && pwd)"\n'
            'cd "${TASK_REPO_ROOT:-$_CP_REPO_DEFAULT}"\n\n'
            "# Stage the agent-authored answer.json for the oracle — no stdout fallback.\n"
            'if [ -f answer.json ]; then\n'
            '    cp answer.json "$TASK_DIR/answer.json"\n'
            "fi\n\n"
            'python3 "$SCRIPT_DIR/oracle.py" "$TASK_DIR"\n'
        )
    else:
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
