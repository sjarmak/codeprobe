"""Org-scale task mining — comprehension/IR tasks with oracle verification.

Generates tasks from structural scan results. The scanner lives in
``org_scale_scanner.py``; oracle comparison in ``org_scale_oracle.py``.

ZFC compliant:
- Scanner does structural detection (globs + regex) — mechanism only.
- LLM generates question text from scan results — semantic judgment.
- Ground truth is scanner output — LLM never touches it.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from codeprobe.mining.curator import CurationResult
from codeprobe.mining.org_scale_families import (
    CROSS_REPO_DEP_TRACE,
    FAMILIES,
    TaskFamily,
)
from codeprobe.mining.org_scale_oracle import (
    extract_answer,
    normalize_path,
    oracle_check,
)
from codeprobe.mining.org_scale_scanner import (
    FamilyScanResult,
    PatternHit,
    discover_top_imports,
    find_callers_of_symbols,
    get_head_sha,
    get_tracked_files,
    scan_repo,
)
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

logger = logging.getLogger(__name__)

# Re-export for backward compatibility
__all__ = [
    "FamilyScanResult",
    "OrgScaleMineResult",
    "PatternHit",
    "extract_answer",
    "generate_org_scale_task",
    "mine_org_scale_tasks",
    "normalize_path",
    "oracle_check",
    "scan_repo",
]


@dataclass(frozen=True)
class OrgScaleMineResult:
    """Result of mine_org_scale_tasks()."""

    tasks: list[Task]
    scan_results: list[FamilyScanResult]


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
            f"reference symbols defined in the matched files. Ask the agent "
            f"to find files that USE or CALL the patterns, not just the "
            f"matches themselves.\n"
            f"Sample caller files: {', '.join(list(multi_hop_files)[:5])}"
        )
    return _TASK_GEN_PROMPT.format(
        family_name=scan_result.family.name,
        family_description=scan_result.family.description,
        repo_name=scan_result.repo_paths[0].name,
        language=language,
        file_count=len(scan_result.matched_files),
        sample_hits=sample_hits,
        multi_hop_context=multi_hop_context,
    )


def _guess_language(scan_result: FamilyScanResult) -> str:
    """Guess primary language from file extensions in scan hits."""
    from codeprobe.mining._lang import guess_language_from_extensions

    extensions = [
        Path(h.file_path).suffix for h in scan_result.hits if Path(h.file_path).suffix
    ]
    return guess_language_from_extensions(extensions)


def _deterministic_question(
    family: TaskFamily,
    scan_result: FamilyScanResult,
    is_multi_hop: bool,
) -> tuple[str, str]:
    """Generate a deterministic question without LLM (--no-llm fallback)."""
    patterns_str = ", ".join(f"`{p}`" for p in family.content_patterns[:3])
    repo_name = scan_result.repo_paths[0].name

    if is_multi_hop:
        return (
            f"Find callers of {family.name} patterns in {repo_name}",
            f"In the {repo_name} repository, find all source files that "
            f"call or reference symbols defined in files matching the "
            f"patterns {patterns_str}. List the caller file paths, one per "
            f"line. Do not include the files containing the patterns "
            f"themselves — only files that USE those symbols.",
        )
    return (
        f"Find {family.name} patterns in {repo_name}",
        f"In the {repo_name} repository, find all files containing "
        f"matches for the patterns {patterns_str}. List the file paths, "
        f"one per line.",
    )


def _llm_question(
    scan_result: FamilyScanResult,
    language: str,
    multi_hop_files: frozenset[str] | None,
    is_multi_hop: bool,
) -> tuple[str, str, str, bool]:
    """Call LLM for question generation. Returns (heading, question, difficulty, succeeded)."""
    from codeprobe.core.llm import LLMError, LLMRequest, call_claude

    prompt = _build_task_gen_prompt(scan_result, language, multi_hop_files)
    try:
        response = call_claude(
            LLMRequest(prompt=prompt, model="haiku", timeout_seconds=30)
        )
        data = json.loads(response.text)
        heading = data.get("heading", f"{scan_result.family.name} task")
        question = data.get("question", "")
        difficulty = data.get("difficulty", "medium")
        if difficulty not in ("easy", "medium", "hard"):
            difficulty = "medium"
        if question:
            return heading, question, difficulty, True
    except (LLMError, json.JSONDecodeError, KeyError) as exc:
        logger.warning(
            "LLM task generation failed for %s: %s", scan_result.family.name, exc
        )

    heading, question = _deterministic_question(
        scan_result.family, scan_result, is_multi_hop
    )
    return heading, question, "medium", False


def generate_org_scale_task(
    scan_result: FamilyScanResult,
    *,
    multi_hop_files: frozenset[str] | None = None,
    no_llm: bool = False,
    curation_result: CurationResult | None = None,
) -> Task | None:
    """Generate a single org-scale task from scan results.

    When *curation_result* is provided, its files are used for ground truth
    and the per-file tier mapping is populated on ``TaskVerification.oracle_tiers``.
    """
    language = _guess_language(scan_result)
    family = scan_result.family
    is_multi_hop = multi_hop_files is not None

    # When curation is provided, use curated files as ground truth.
    if curation_result is not None:
        ground_truth_files = curation_result.matched_files
        oracle_tiers = {cf.path: cf.tier for cf in curation_result.files}
    else:
        ground_truth_files = (
            multi_hop_files if is_multi_hop else scan_result.matched_files
        )
        oracle_tiers = {}

    if not ground_truth_files:
        return None

    task_id_source = f"{family.name}-{scan_result.commit_sha[:8]}"
    if is_multi_hop:
        task_id_source += "-mh"
    task_id = hashlib.sha256(task_id_source.encode()).hexdigest()[:8]

    if no_llm:
        heading, question = _deterministic_question(family, scan_result, is_multi_hop)
        difficulty = "medium" if is_multi_hop else "easy"
        llm_succeeded = False
    else:
        heading, question, difficulty, llm_succeeded = _llm_question(
            scan_result, language, multi_hop_files, is_multi_hop
        )

    return Task(
        id=task_id,
        repo=scan_result.repo_paths[0].name,
        metadata=TaskMetadata(
            name=f"org-{task_id}",
            difficulty=difficulty,
            description=f"{family.name}: {len(ground_truth_files)} files",
            language=language,
            category=family.name,
            org_scale=True,
            issue_title=heading,
            issue_body=question,
            enrichment_source="llm" if llm_succeeded else "",
            ground_truth_commit=scan_result.commit_sha,
        ),
        verification=TaskVerification(
            type="oracle",
            command="bash tests/test.sh",
            reward_type="continuous",
            oracle_type="file_list",
            oracle_answer=tuple(sorted(ground_truth_files)),
            oracle_tiers=oracle_tiers,
        ),
        instruction_variant_path="instruction_discovery.md",
    )


def _build_dep_trace_task(
    pkg_name: str,
    importing_files: frozenset[str],
    repo_path: Path,
    commit_sha: str,
    language: str,
) -> Task:
    """Build a dep-trace task for a specific package."""
    repo_name = repo_path.name
    task_id = hashlib.sha256(
        f"dep-trace-{pkg_name}-{commit_sha[:8]}".encode()
    ).hexdigest()[:8]

    return Task(
        id=task_id,
        repo=repo_name,
        metadata=TaskMetadata(
            name=f"org-{task_id}",
            difficulty="medium",
            description=f"cross-repo-dep-trace: {pkg_name} ({len(importing_files)} files)",
            language=language,
            category="cross-repo-dep-trace",
            org_scale=True,
            issue_title=f"Find files importing {pkg_name} in {repo_name}",
            issue_body=(
                f"In the {repo_name} repository, find all source files that "
                f"import the package `{pkg_name}`. List the file paths, "
                f"one per line."
            ),
            ground_truth_commit=commit_sha,
        ),
        verification=TaskVerification(
            type="oracle",
            command="bash tests/test.sh",
            reward_type="continuous",
            oracle_type="file_list",
            oracle_answer=tuple(sorted(importing_files)),
        ),
        instruction_variant_path="instruction_discovery.md",
    )


# ---------------------------------------------------------------------------
# Main mining function
# ---------------------------------------------------------------------------


def _mine_pattern_families(
    scan_results: list[FamilyScanResult],
    repo_paths: list[Path],
    tracked_files: frozenset[str],
    *,
    count: int,
    no_llm: bool,
    max_files: int,
    include_multi_hop: bool,
) -> list[Task]:
    """Generate tasks from pattern-based scan results (non-dep-trace)."""
    tasks: list[Task] = []
    for scan_result in scan_results:
        if len(tasks) >= count:
            break
        task = generate_org_scale_task(scan_result, no_llm=no_llm)
        if task is not None:
            tasks.append(task)

        if include_multi_hop and scan_result.family.multi_hop and len(tasks) < count:
            language = _guess_language(scan_result)
            multi_hop_files = find_callers_of_symbols(
                repo_paths,
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
    return tasks


def _mine_dep_trace(
    repo_paths: list[Path],
    tracked_files: frozenset[str],
    scan_results: list[FamilyScanResult],
    *,
    count: int,
    max_files: int,
) -> list[Task]:
    """Generate dep-trace tasks by discovering top imported packages."""
    primary_repo = repo_paths[0]
    commit_sha = get_head_sha(primary_repo)
    dep_language = _guess_repo_language(tracked_files)
    top_packages = discover_top_imports(
        repo_paths,
        tracked_files,
        dep_language,
        max_files=max_files,
    )
    tasks: list[Task] = []
    for pkg_name, importing_files in top_packages:
        if len(tasks) >= count:
            break
        tasks.append(
            _build_dep_trace_task(
                pkg_name,
                importing_files,
                primary_repo,
                commit_sha,
                dep_language,
            )
        )
        scan_results.append(
            FamilyScanResult(
                family=CROSS_REPO_DEP_TRACE,
                hits=tuple(
                    PatternHit(f, 0, f'import "{pkg_name}"', pkg_name)
                    for f in list(importing_files)[:10]
                ),
                repo_paths=tuple(repo_paths),
                commit_sha=commit_sha,
                matched_files=importing_files,
            )
        )
        logger.info(
            "Dep-trace: %s imported by %d files", pkg_name, len(importing_files)
        )
    return tasks


def mine_org_scale_tasks(
    repo_paths: list[Path],
    *,
    count: int = 5,
    families: tuple[TaskFamily, ...] | None = None,
    no_llm: bool = False,
    max_files: int = 50_000,
    include_multi_hop: bool = True,
    scan_timeout: int = 60,
) -> OrgScaleMineResult:
    """Mine org-scale comprehension tasks from one or more repositories.

    Args:
        repo_paths: One or more local repo directories to scan.
        count: Maximum number of tasks to generate.
        families: Restrict to specific families (default: all).
        no_llm: Skip LLM question generation, use deterministic fallback.
        max_files: Cap on files to scan per family.
        include_multi_hop: Generate multi-hop task variants.
        scan_timeout: Per-family scan timeout in seconds.
    """
    all_families = families or FAMILIES
    non_dep = tuple(f for f in all_families if f.name != "cross-repo-dep-trace")
    want_dep = any(f.name == "cross-repo-dep-trace" for f in all_families)

    # Merge tracked files from all repos
    all_tracked: frozenset[str] = frozenset()
    for rp in repo_paths:
        all_tracked = all_tracked | get_tracked_files(rp)

    scan_results = scan_repo(
        repo_paths,
        non_dep,
        max_files=max_files,
        tracked_files=all_tracked,
        timeout_seconds=scan_timeout,
    )

    # Build multi-repo commits mapping
    commits = tuple((rp.name, get_head_sha(rp)) for rp in repo_paths)

    tasks = _mine_pattern_families(
        scan_results,
        repo_paths,
        all_tracked,
        count=count,
        no_llm=no_llm,
        max_files=max_files,
        include_multi_hop=include_multi_hop,
    )

    # Stamp multi-repo commits onto tasks when there are multiple repos
    if len(repo_paths) > 1:
        tasks = [_stamp_multi_repo_commits(t, commits) for t in tasks]

    if want_dep and len(tasks) < count:
        dep_tasks = _mine_dep_trace(
            repo_paths,
            all_tracked,
            scan_results,
            count=count - len(tasks),
            max_files=max_files,
        )
        if len(repo_paths) > 1:
            dep_tasks = [_stamp_multi_repo_commits(t, commits) for t in dep_tasks]
        tasks.extend(dep_tasks)

    if not tasks:
        logger.info("No org-scale tasks generated from %s", repo_paths)

    return OrgScaleMineResult(tasks=tasks[:count], scan_results=scan_results)


def _stamp_multi_repo_commits(task: Task, commits: tuple[tuple[str, str], ...]) -> Task:
    """Return a new Task with ground_truth_commits set for multi-repo."""
    return Task(
        id=task.id,
        repo=task.repo,
        metadata=TaskMetadata(
            name=task.metadata.name,
            difficulty=task.metadata.difficulty,
            description=task.metadata.description,
            license=task.metadata.license,
            language=task.metadata.language,
            category=task.metadata.category,
            org_scale=task.metadata.org_scale,
            mcp_suite=task.metadata.mcp_suite,
            tags=task.metadata.tags,
            estimated_duration_sec=task.metadata.estimated_duration_sec,
            resource_tier=task.metadata.resource_tier,
            issue_title=task.metadata.issue_title,
            issue_body=task.metadata.issue_body,
            quality_score=task.metadata.quality_score,
            enrichment_source=task.metadata.enrichment_source,
            ground_truth_commit=task.metadata.ground_truth_commit,
            ground_truth_commits=commits,
        ),
        verification=task.verification,
        instruction_path=task.instruction_path,
        instruction_variant_path=task.instruction_variant_path,
        time_limit_sec=task.time_limit_sec,
        verification_modes=task.verification_modes,
    )


def _guess_repo_language(tracked_files: frozenset[str]) -> str:
    """Guess repo language from file extensions."""
    from codeprobe.mining._lang import guess_language_from_paths

    return guess_language_from_paths(tracked_files)


def validate_ground_truth_sample(
    task: Task,
    repo_paths: list[Path],
    *,
    sample_size: int = 5,
) -> bool | None:
    """Best-effort LLM validation of ground truth for a single task.

    Samples up to ``sample_size`` matches and ``sample_size`` non-matches from
    the repo, asks the LLM to confirm whether each file should be in the ground
    truth. Logs a warning if the LLM disagrees on more than 1 file.

    Returns True if validation passed, False if disagreements found,
    None if LLM was unavailable (skipped silently).
    """
    try:
        from codeprobe.core.llm import LLMError, LLMRequest, call_claude, llm_available

        if not llm_available():
            return None
    except ImportError:
        return None

    gt_files = set(task.verification.oracle_answer)
    if not gt_files:
        return True

    # Gather non-match files from tracked files
    all_tracked: set[str] = set()
    for rp in repo_paths:
        all_tracked.update(get_tracked_files(rp))
    non_matches = sorted(all_tracked - gt_files)

    import random

    rng = random.Random(42)
    match_sample = sorted(rng.sample(sorted(gt_files), min(sample_size, len(gt_files))))
    non_match_sample = (
        sorted(rng.sample(non_matches, min(sample_size, len(non_matches))))
        if non_matches
        else []
    )

    prompt = (
        f"You are validating ground truth for a code comprehension task.\n\n"
        f"Task category: {task.metadata.category}\n"
        f"Task description: {task.metadata.description}\n"
        f"Question: {task.metadata.issue_body}\n\n"
        f"Files claimed to MATCH (should be in answer):\n"
        + "\n".join(f"  {f}" for f in match_sample)
        + f"\n\nFiles claimed to NOT MATCH (should not be in answer):\n"
        + "\n".join(f"  {f}" for f in non_match_sample)
        + "\n\nFor each file, respond with JSON: "
        '{"disagreements": ["file1.py", "file2.py"]} '
        "listing only files where you disagree with the classification. "
        "Empty list means you agree with all classifications.\n"
        "Respond ONLY with the JSON object."
    )

    try:
        response = call_claude(
            LLMRequest(prompt=prompt, model="haiku", timeout_seconds=30)
        )
        data = json.loads(response.text)
        disagreements = data.get("disagreements", [])
        if len(disagreements) > 1:
            logger.warning(
                "LLM ground truth validation: %d disagreements for task %s: %s",
                len(disagreements),
                task.id,
                disagreements,
            )
            return False
        return True
    except (LLMError, json.JSONDecodeError, KeyError) as exc:
        logger.debug("LLM ground truth validation skipped: %s", exc)
        return None
