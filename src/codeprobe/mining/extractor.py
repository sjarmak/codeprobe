"""Task extraction from git history — mine eval tasks from merge commits."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from codeprobe.mining.sources import RepoSource, detect_source
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

logger = logging.getLogger(__name__)

_DIFF_STAT_TIMEOUT = 15
_GIT_LOG_TIMEOUT = 15


@dataclass(frozen=True)
class MergedPR:
    """A merged pull request extracted from git log."""

    sha: str
    title: str
    merge_commit: str


def list_merged_prs(source: RepoSource, path: Path, limit: int = 20) -> list[MergedPR]:
    """List recently merged PRs/MRs from git log.

    For local repos without a remote API, parse git log for merge commits.
    """
    try:
        result = subprocess.run(
            ["git", "log", "--merges", "--oneline", "--format=%H %s", f"-n{limit}"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=_GIT_LOG_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("git log failed: %s", result.stderr.strip())
            return []
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("Failed to list merge commits: %s", exc)
        return []

    prs: list[MergedPR] = []
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split(" ", 1)
        if len(parts) < 2:
            continue
        sha, title = parts
        prs.append(MergedPR(sha=sha, title=title, merge_commit=sha))
    return prs


def _get_changed_files(merge_sha: str, repo_path: Path) -> list[str]:
    """Get the list of changed files for a merge commit."""
    try:
        result = subprocess.run(
            ["git", "diff", f"{merge_sha}^..{merge_sha}", "--name-only"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_DIFF_STAT_TIMEOUT,
        )
        if result.returncode != 0:
            logger.warning("git diff failed for %s: %s", merge_sha, result.stderr.strip())
            return []
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("git diff error for %s: %s", merge_sha, exc)
        return []

    return [f for f in result.stdout.strip().splitlines() if f.strip()]


def _estimate_difficulty(changed_files: list[str]) -> str:
    """Estimate task difficulty based on the number of changed files."""
    count = len(changed_files)
    if count <= 3:
        return "easy"
    if count <= 10:
        return "medium"
    return "hard"


def _find_test_files(changed_files: list[str]) -> list[str]:
    """Return files that look like tests from the changed file list."""
    return [
        f
        for f in changed_files
        if "test" in f.lower() or "spec" in f.lower()
    ]


def extract_task_from_merge(merge_sha: str, repo_path: Path) -> Task | None:
    """Extract an eval task from a merge commit.

    Returns None if the merge has no test files (can't verify the task).
    """
    changed_files = _get_changed_files(merge_sha, repo_path)
    if not changed_files:
        return None

    test_files = _find_test_files(changed_files)
    if not test_files:
        return None

    short_sha = merge_sha[:8]
    difficulty = _estimate_difficulty(changed_files)

    # Detect primary language from file extensions
    extensions = [Path(f).suffix for f in changed_files if Path(f).suffix]
    language = _guess_language(extensions)

    metadata = TaskMetadata(
        name=f"merge-{short_sha}",
        difficulty=difficulty,
        description=f"Reproduce changes from merge commit {short_sha}",
        language=language,
        category="sdlc",
    )

    # Build a verification command that runs the detected test files
    test_command = "bash tests/test.sh"
    verification = TaskVerification(
        type="test_script",
        command=test_command,
        reward_type="binary",
    )

    return Task(
        id=short_sha,
        repo=repo_path.name,
        metadata=metadata,
        verification=verification,
    )


def _guess_language(extensions: list[str]) -> str:
    """Guess the primary language from file extensions."""
    from collections import Counter

    lang_map: dict[str, str] = {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".rb": "ruby",
        ".swift": "swift",
        ".kt": "kotlin",
        ".cpp": "cpp",
        ".c": "c",
        ".php": "php",
    }
    counts = Counter(lang_map[ext] for ext in extensions if ext in lang_map)
    if not counts:
        return ""
    return counts.most_common(1)[0][0]


def mine_tasks(
    path: Path,
    count: int = 5,
    source_hint: str = "auto",
) -> list[Task]:
    """Mine eval tasks from a repository.

    1. Detect or use provided source
    2. List merged PRs
    3. Extract tasks from each merge
    4. Filter out None results
    5. Return up to *count* tasks
    """
    if source_hint != "auto":
        source = RepoSource(host=source_hint, owner="", repo=path.name, remote_url="")
    else:
        source = detect_source(path)
    logger.info("Detected source: %s (%s/%s)", source.host, source.owner, source.repo)

    # Fetch more merges than needed to account for those without tests
    prs = list_merged_prs(source, path, limit=count * 4)
    if not prs:
        logger.info("No merge commits found in %s", path)
        return []

    tasks: list[Task] = []
    for pr in prs:
        if len(tasks) >= count:
            break
        task = extract_task_from_merge(pr.merge_commit, path)
        if task is not None:
            tasks.append(task)

    logger.info("Mined %d tasks from %d merge commits", len(tasks), len(prs))
    return tasks
