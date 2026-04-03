"""Org-scale task mining — comprehension/IR tasks with oracle verification.

Scans codebases for structural patterns (deprecated APIs, security configs,
import dependencies) and generates comprehension tasks with deterministic
ground truth.

ZFC compliant:
- Scanner does structural detection (globs + regex) — mechanism only.
- LLM generates question text from scan results — semantic judgment.
- Ground truth is scanner output — LLM never touches it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

from codeprobe.mining.org_scale_families import FAMILIES, TaskFamily
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 15
_MAX_LINE_LEN = 500  # Skip extremely long lines (minified files)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatternHit:
    """A single pattern match in a file."""

    file_path: str  # relative to repo root
    line_number: int
    matched_text: str  # the line content (truncated)
    pattern_used: str  # which regex matched


@dataclass(frozen=True)
class FamilyScanResult:
    """Results of scanning a repo for one task family."""

    family: TaskFamily
    hits: tuple[PatternHit, ...]
    repo_path: Path
    commit_sha: str
    matched_files: frozenset[str]  # deduplicated file paths


@dataclass(frozen=True)
class OrgScaleMineResult:
    """Result of mine_org_scale_tasks()."""

    tasks: list[Task]
    scan_results: list[FamilyScanResult]


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _get_head_sha(repo_path: Path) -> str:
    """Get the HEAD commit SHA for a repo."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return ""


def _get_tracked_files(repo_path: Path) -> frozenset[str]:
    """Get the set of git-tracked files (respects .gitignore)."""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            return frozenset(f for f in result.stdout.strip().splitlines() if f.strip())
    except (subprocess.TimeoutExpired, OSError):
        pass
    return frozenset()


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


def scan_repo_for_family(
    repo_path: Path,
    family: TaskFamily,
    *,
    max_files: int = 50_000,
    tracked_files: frozenset[str] | None = None,
) -> FamilyScanResult:
    """Scan a repo for pattern matches belonging to a task family.

    Uses git ls-files for gitignore-respecting file discovery, then applies
    content regex patterns. Returns results capped at ``family.max_hits``.

    Args:
        repo_path: Root of the git repository.
        family: Task family with glob and content patterns.
        max_files: Maximum files to scan (prevents OOM on huge repos).
        tracked_files: Pre-computed ``git ls-files`` output (optimization).
    """
    commit_sha = _get_head_sha(repo_path)

    if tracked_files is None:
        tracked_files = _get_tracked_files(repo_path)

    # Filter tracked files by family's glob patterns
    compiled_globs = family.glob_patterns
    candidate_files: list[str] = []
    for f in tracked_files:
        if any(_matches_glob(f, g) for g in compiled_globs):
            candidate_files.append(f)
        if len(candidate_files) >= max_files:
            logger.warning(
                "Hit max_files=%d cap scanning %s for %s",
                max_files,
                repo_path.name,
                family.name,
            )
            break

    # Compile content patterns
    compiled_patterns = []
    for p in family.content_patterns:
        try:
            compiled_patterns.append((p, re.compile(p)))
        except re.error:
            logger.warning("Invalid regex in family %s: %s", family.name, p)

    # Scan file contents
    hits: list[PatternHit] = []
    matched_files: set[str] = set()

    for file_path in candidate_files:
        full_path = repo_path / file_path
        if not full_path.is_file():
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        for line_num, line in enumerate(content.splitlines(), 1):
            if len(line) > _MAX_LINE_LEN:
                continue
            for pattern_str, pattern_re in compiled_patterns:
                if pattern_re.search(line):
                    hits.append(
                        PatternHit(
                            file_path=file_path,
                            line_number=line_num,
                            matched_text=line.strip()[:200],
                            pattern_used=pattern_str,
                        )
                    )
                    matched_files.add(file_path)
                    break  # one match per line is enough

        if len(hits) >= family.max_hits:
            break

    return FamilyScanResult(
        family=family,
        hits=tuple(hits[: family.max_hits]),
        repo_path=repo_path,
        commit_sha=commit_sha,
        matched_files=frozenset(matched_files),
    )


def scan_repo(
    repo_path: Path,
    families: tuple[TaskFamily, ...] | None = None,
    *,
    max_files: int = 50_000,
) -> list[FamilyScanResult]:
    """Scan a repo for all task families. Returns results with enough hits."""
    if families is None:
        families = FAMILIES

    tracked_files = _get_tracked_files(repo_path)
    results: list[FamilyScanResult] = []

    for family in families:
        result = scan_repo_for_family(
            repo_path, family, max_files=max_files, tracked_files=tracked_files
        )
        if len(result.matched_files) >= family.min_hits:
            results.append(result)
            logger.info(
                "Family %s: %d files matched (%d hits)",
                family.name,
                len(result.matched_files),
                len(result.hits),
            )
        else:
            logger.info(
                "Family %s: %d files (below min_hits=%d, skipping)",
                family.name,
                len(result.matched_files),
                family.min_hits,
            )

    return results


def _matches_glob(file_path: str, glob_pattern: str) -> bool:
    """Check if a file path matches a glob pattern (simple implementation)."""
    from fnmatch import fnmatch

    # Handle **/ prefix
    if glob_pattern.startswith("**/"):
        pattern = glob_pattern[3:]
        return (
            fnmatch(file_path, pattern)
            or fnmatch(file_path, f"**/{pattern}")
            or any(
                fnmatch(part, pattern.split("/")[-1]) for part in file_path.split("/")
            )
        )
    return fnmatch(file_path, glob_pattern)


# ---------------------------------------------------------------------------
# Multi-hop scan extensions
# ---------------------------------------------------------------------------


def _find_callers_of_symbols(
    repo_path: Path,
    symbol_files: frozenset[str],
    tracked_files: frozenset[str],
    language: str,
    *,
    max_files: int = 50_000,
) -> frozenset[str]:
    """Find files that call/import symbols defined in the given files.

    This is the multi-hop extension: given files containing deprecated
    annotations, find OTHER files that reference those symbols.
    """
    # Extract likely symbol names from the deprecated files
    symbols: set[str] = set()
    for file_path in symbol_files:
        full_path = repo_path / file_path
        if not full_path.is_file():
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        # Extract function/class/type names near deprecated annotations
        for line in content.splitlines():
            # Go: func/type after deprecated comment
            if language == "go":
                m = re.search(r"^func\s+(?:\([^)]+\)\s+)?(\w+)", line)
                if m:
                    symbols.add(m.group(1))
                m = re.search(r"^type\s+(\w+)", line)
                if m:
                    symbols.add(m.group(1))
            # Python: def/class after @deprecated
            elif language == "python":
                m = re.search(r"^(?:def|class)\s+(\w+)", line)
                if m:
                    symbols.add(m.group(1))
            # Java: method/class after @Deprecated
            elif language == "java":
                m = re.search(
                    r"(?:public|private|protected)?\s*(?:static\s+)?(?:\w+\s+)+(\w+)\s*\(",
                    line,
                )
                if m:
                    symbols.add(m.group(1))

    if not symbols:
        return frozenset()

    # Find files referencing these symbols (excluding the definition files)
    caller_files: set[str] = set()
    symbol_pattern = re.compile(
        r"\b(" + "|".join(re.escape(s) for s in symbols) + r")\b"
    )

    scanned = 0
    for file_path in tracked_files:
        if file_path in symbol_files:
            continue
        if scanned >= max_files:
            break
        full_path = repo_path / file_path
        if not full_path.is_file():
            continue
        # Only scan source files
        if not any(
            file_path.endswith(ext)
            for ext in (".go", ".py", ".java", ".ts", ".js", ".rs")
        ):
            continue
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1

        if symbol_pattern.search(content):
            caller_files.add(file_path)

    return frozenset(caller_files)


# ---------------------------------------------------------------------------
# LLM task generation
# ---------------------------------------------------------------------------

_TASK_GEN_PROMPT = """\
You are generating a comprehension task for an AI coding agent benchmark.

Given structural scan results from a codebase, write a clear question that
tests the agent's ability to navigate and understand the code.

## Scan Results
Family: {family_name}
Description: {family_description}
Repository: {repo_name}
Language: {language}
Files matched: {file_count}
Sample matches (first 10):
{sample_hits}

{multi_hop_context}

## Instructions
Produce a JSON object with:
- "question": A clear, specific question scoped to the scanner's literal \
pattern. For single-hop: "Which files contain X?" For multi-hop: "Which files \
call/use the deprecated symbols found in X?" The question must be answerable \
by listing file paths.
- "heading": A short title for the task (5-10 words).
- "difficulty": One of "easy", "medium", "hard".
- "is_multi_hop": true if this requires tracing relationships, false if \
single-file grep suffices.

IMPORTANT: Do NOT include the answer in the question. The question must be \
solvable by the agent navigating the codebase.

Respond ONLY with the JSON object, no markdown fences.
"""


def _build_task_gen_prompt(
    scan_result: FamilyScanResult,
    language: str,
    multi_hop_files: frozenset[str] | None = None,
) -> str:
    """Build the LLM prompt for generating a task question."""
    sample_hits = "\n".join(
        f"  {h.file_path}:{h.line_number} — {h.matched_text[:100]}"
        for h in scan_result.hits[:10]
    )

    multi_hop_context = ""
    if multi_hop_files:
        multi_hop_context = (
            f"\n## Multi-Hop Extension\n"
            f"The scanner also found {len(multi_hop_files)} files that "
            f"reference symbols defined in the matched files. This enables "
            f"a harder question: ask the agent to find files that USE or CALL "
            f"the patterns found in the initial matches, not just the matches "
            f"themselves.\n"
            f"Sample caller files: {', '.join(list(multi_hop_files)[:5])}"
        )

    return _TASK_GEN_PROMPT.format(
        family_name=scan_result.family.name,
        family_description=scan_result.family.description,
        repo_name=scan_result.repo_path.name,
        language=language,
        file_count=len(scan_result.matched_files),
        sample_hits=sample_hits,
        multi_hop_context=multi_hop_context,
    )


def _guess_language_from_hits(scan_result: FamilyScanResult) -> str:
    """Guess primary language from file extensions in scan hits."""
    from collections import Counter

    ext_map = {
        ".go": "go",
        ".py": "python",
        ".java": "java",
        ".ts": "typescript",
        ".js": "javascript",
        ".rs": "rust",
        ".kt": "kotlin",
        ".cpp": "cpp",
        ".c": "c",
        ".rb": "ruby",
    }
    exts = Counter(
        Path(h.file_path).suffix for h in scan_result.hits if Path(h.file_path).suffix
    )
    if exts:
        top_ext = exts.most_common(1)[0][0]
        return ext_map.get(top_ext, "unknown")
    return "unknown"


def generate_org_scale_task(
    scan_result: FamilyScanResult,
    *,
    multi_hop_files: frozenset[str] | None = None,
    no_llm: bool = False,
) -> Task | None:
    """Generate a single org-scale task from scan results.

    When *no_llm* is True, generates a deterministic template question
    without calling the LLM.
    """
    language = _guess_language_from_hits(scan_result)
    family = scan_result.family

    # Determine ground truth
    if multi_hop_files:
        ground_truth_files = multi_hop_files
        is_multi_hop = True
    else:
        ground_truth_files = scan_result.matched_files
        is_multi_hop = False

    if not ground_truth_files:
        return None

    # Generate task ID from family + commit
    task_id_source = f"{family.name}-{scan_result.commit_sha[:8]}"
    if is_multi_hop:
        task_id_source += "-mh"
    task_id = hashlib.sha256(task_id_source.encode()).hexdigest()[:8]

    llm_succeeded = False
    if no_llm:
        # Deterministic fallback
        heading, question = _deterministic_question(family, scan_result, is_multi_hop)
        difficulty = "medium" if is_multi_hop else "easy"
    else:
        # LLM-generated question
        from codeprobe.core.llm import LLMError, LLMRequest, call_claude

        prompt = _build_task_gen_prompt(scan_result, language, multi_hop_files)
        try:
            response = call_claude(
                LLMRequest(prompt=prompt, model="haiku", timeout_seconds=30)
            )
            data = json.loads(response.text)
            heading = data.get("heading", f"{family.name} task")
            question = data.get("question", "")
            difficulty = data.get("difficulty", "medium")
            if difficulty not in ("easy", "medium", "hard"):
                difficulty = "medium"
            if not question:
                heading, question = _deterministic_question(
                    family, scan_result, is_multi_hop
                )
            else:
                llm_succeeded = True
        except (LLMError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("LLM task generation failed for %s: %s", family.name, exc)
            heading, question = _deterministic_question(
                family, scan_result, is_multi_hop
            )
            difficulty = "medium"

    metadata = TaskMetadata(
        name=f"org-{task_id}",
        difficulty=difficulty,
        description=question,
        language=language,
        category=family.name,
        org_scale=True,
        issue_title=heading,
        issue_body=question,
        enrichment_source="llm" if llm_succeeded else "",
        ground_truth_commit=scan_result.commit_sha,
    )

    verification = TaskVerification(
        type="oracle",
        command="bash tests/test.sh",
        reward_type="continuous",
        oracle_type="file_list",
        oracle_answer=tuple(sorted(ground_truth_files)),
    )

    return Task(
        id=task_id,
        repo=scan_result.repo_path.name,
        metadata=metadata,
        verification=verification,
    )


def _deterministic_question(
    family: TaskFamily,
    scan_result: FamilyScanResult,
    is_multi_hop: bool,
) -> tuple[str, str]:
    """Generate a deterministic question without LLM (--no-llm fallback)."""
    patterns_str = ", ".join(f"`{p}`" for p in family.content_patterns[:3])
    repo_name = scan_result.repo_path.name

    if is_multi_hop:
        heading = f"Find callers of {family.name} patterns in {repo_name}"
        question = (
            f"In the {repo_name} repository, find all source files that "
            f"call or reference symbols defined in files matching the "
            f"patterns {patterns_str}. List the caller file paths, one per "
            f"line. Do not include the files containing the patterns "
            f"themselves — only files that USE those symbols."
        )
    else:
        heading = f"Find {family.name} patterns in {repo_name}"
        question = (
            f"In the {repo_name} repository, find all files containing "
            f"matches for the patterns {patterns_str}. List the file paths, "
            f"one per line."
        )
    return heading, question


# ---------------------------------------------------------------------------
# Main mining function
# ---------------------------------------------------------------------------


def mine_org_scale_tasks(
    repo_path: Path,
    *,
    count: int = 5,
    families: tuple[TaskFamily, ...] | None = None,
    no_llm: bool = False,
    max_files: int = 50_000,
    include_multi_hop: bool = True,
) -> OrgScaleMineResult:
    """Mine org-scale comprehension tasks from a repository.

    Scans for structural patterns, generates questions (via LLM or
    deterministic templates), and produces tasks with oracle ground truth.

    Args:
        repo_path: Path to the git repository.
        count: Maximum number of tasks to generate.
        families: Task families to scan for (default: all Phase 1 families).
        no_llm: Skip LLM, use deterministic question templates.
        max_files: Maximum files to scan per family.
        include_multi_hop: Generate multi-hop task variants.
    """
    scan_results = scan_repo(repo_path, families, max_files=max_files)

    if not scan_results:
        logger.info("No patterns found in %s", repo_path)
        return OrgScaleMineResult(tasks=[], scan_results=[])

    tracked_files = _get_tracked_files(repo_path)
    tasks: list[Task] = []

    for scan_result in scan_results:
        if len(tasks) >= count:
            break

        language = _guess_language_from_hits(scan_result)

        # Single-hop task
        task = generate_org_scale_task(scan_result, no_llm=no_llm)
        if task is not None:
            tasks.append(task)

        # Multi-hop task (if family supports it and we have room)
        if include_multi_hop and scan_result.family.multi_hop and len(tasks) < count:
            multi_hop_files = _find_callers_of_symbols(
                repo_path,
                scan_result.matched_files,
                tracked_files,
                language,
                max_files=max_files,
            )
            if len(multi_hop_files) >= 3:
                mh_task = generate_org_scale_task(
                    scan_result,
                    multi_hop_files=multi_hop_files,
                    no_llm=no_llm,
                )
                if mh_task is not None:
                    tasks.append(mh_task)

    logger.info("Generated %d org-scale tasks from %s", len(tasks), repo_path)
    return OrgScaleMineResult(tasks=tasks[:count], scan_results=scan_results)


# ---------------------------------------------------------------------------
# Oracle comparison (used by oracle-check CLI)
# ---------------------------------------------------------------------------


def normalize_path(path: str) -> str:
    """Normalize a file path for oracle comparison.

    Strips common prefixes, normalizes separators, and removes leading dots.
    """
    # Normalize separators to forward slash (Windows compat)
    p = path.replace("\\", "/")
    # Strip common prefixes
    for prefix in ("./", "/workspace/", "/tmp/", "/app/"):
        if p.startswith(prefix):
            p = p[len(prefix) :]
    # Strip leading slash
    p = p.lstrip("/")
    # Strip trailing whitespace/newline
    p = p.strip()
    return p


def extract_answer(task_dir: Path) -> list[str]:
    """Extract the agent's answer from answer.txt in the task directory.

    Returns a list of normalized, deduplicated file paths.
    """
    answer_file = task_dir / "answer.txt"
    if not answer_file.exists():
        logger.warning("No answer.txt found in %s", task_dir)
        return []

    try:
        raw = answer_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Failed to read answer.txt: %s", exc)
        return []

    paths: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            normalized = normalize_path(line)
            if normalized:
                paths.append(normalized)

    return paths


def oracle_check(
    task_dir: Path,
    *,
    metric: str = "f1",
) -> dict[str, float | str]:
    """Compare agent answer against ground truth.

    Args:
        task_dir: Task directory containing answer.txt and ground_truth.json.
        metric: Primary metric: ``"f1"``, ``"recall"``, ``"precision"``, ``"jaccard"``.

    Returns:
        Dict with ``score`` (primary metric value), ``precision``, ``recall``,
        ``f1``, ``jaccard``, and ``error`` (empty string if no error).
    """
    # Load ground truth
    gt_path = task_dir / "ground_truth.json"
    if not gt_path.exists():
        return {"score": 0.0, "error": f"Missing {gt_path}"}

    try:
        gt_data = json.loads(gt_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"score": 0.0, "error": f"Invalid ground_truth.json: {exc}"}

    expected_raw = gt_data.get("expected", [])
    if not isinstance(expected_raw, list):
        return {"score": 0.0, "error": "ground_truth.json 'expected' is not a list"}

    # Normalize ground truth paths
    expected: frozenset[str] = frozenset(normalize_path(p) for p in expected_raw if p)

    if not expected:
        return {"score": 0.0, "error": "Empty ground truth"}

    # Extract and normalize agent answer
    agent_answer_list = extract_answer(task_dir)
    agent_answer: frozenset[str] = frozenset(agent_answer_list)

    if not agent_answer:
        return {
            "score": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "f1": 0.0,
            "jaccard": 0.0,
            "error": "Empty agent answer (no answer.txt or no valid paths)",
        }

    # Compute metrics using sets (not lists — premortem P0)
    intersection = expected & agent_answer
    intersection_size = len(intersection)

    precision = intersection_size / len(agent_answer) if agent_answer else 0.0
    recall = intersection_size / len(expected) if expected else 0.0
    f1 = (
        2.0 * precision * recall / (precision + recall)
        if (precision + recall) > 0
        else 0.0
    )
    union = expected | agent_answer
    jaccard = intersection_size / len(union) if union else 0.0

    # Invariant check (premortem P0: crash, don't clamp)
    assert 0.0 <= f1 <= 1.0, f"F1 out of bounds: {f1}"
    assert 0.0 <= precision <= 1.0, f"Precision out of bounds: {precision}"
    assert 0.0 <= recall <= 1.0, f"Recall out of bounds: {recall}"

    metrics = {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "jaccard": round(jaccard, 4),
        "intersection_size": intersection_size,
        "expected_size": len(expected),
        "answer_size": len(agent_answer),
        "error": "",
    }

    # Select primary metric
    metric_map = {
        "f1": f1,
        "recall": recall,
        "precision": precision,
        "jaccard": jaccard,
    }
    metrics["score"] = round(metric_map.get(metric, f1), 4)

    return metrics
