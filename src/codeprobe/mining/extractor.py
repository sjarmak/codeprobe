"""Task extraction from git history — mine eval tasks from merge commits."""

from __future__ import annotations

import json as _json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from codeprobe.mining.sources import RepoSource, detect_source
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

logger = logging.getLogger(__name__)

_DIFF_STAT_TIMEOUT = 15
_GIT_LOG_TIMEOUT = 15
_GH_TIMEOUT = 15
_MAX_BODY_LENGTH = 2000
_MAX_LABELS = 10
_MAX_LABEL_LEN = 64
_GH_PR_PATTERN = re.compile(r"[Mm]erge pull request #(\d+)")
_PR_NUMBER_PATTERN = re.compile(r"#(\d+)")
_LABEL_SANITIZE = re.compile(r"[^\w\s,./:-]")


@dataclass(frozen=True)
class MergedPR:
    """A merged pull request extracted from git log."""

    sha: str
    title: str
    merge_commit: str


@dataclass(frozen=True)
class PRMetadata:
    """Metadata extracted from a pull request or merge commit message."""

    title: str
    body: str = ""
    labels: tuple[str, ...] = ()
    source_tier: Literal["api", "commit_message", "bare"] = "bare"


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
            logger.warning(
                "git diff failed for %s: %s", merge_sha, result.stderr.strip()
            )
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
    return [f for f in changed_files if "test" in f.lower() or "spec" in f.lower()]


_DEFAULT_TEST_COMMAND = "bash tests/test.sh"
_ISSUE_REF_PATTERN = re.compile(
    r"(?:#(\d+)|[A-Z][A-Z0-9]+-\d+)"  # GitHub #N or JIRA-style PROJ-123
)


def _build_test_command(language: str, test_files: list[str]) -> str:
    """Build a targeted test command from language and test file paths.

    Supports Python (pytest), Go (go test), and JS/TS (npm test with jest pattern).
    Falls back to the generic test.sh for unsupported languages or empty file lists.
    """
    if not test_files:
        return _DEFAULT_TEST_COMMAND

    if language == "python":
        return f"pytest {' '.join(test_files)}"

    if language == "go":
        # Extract unique package directories from test file paths
        packages = sorted({str(Path(f).parent) for f in test_files})
        go_paths = " ".join(f"./{pkg}/..." for pkg in packages)
        return f"go test {go_paths}"

    if language in ("javascript", "typescript"):
        # Use the first test file's basename as the pattern
        pattern = Path(test_files[0]).name
        return f"npm test -- --testPathPattern={pattern}"

    return _DEFAULT_TEST_COMMAND


def _fetch_pr_metadata_from_api(
    repo_path: Path,
    source: RepoSource,
    merge_title: str,
) -> PRMetadata | None:
    """Fetch PR metadata via host CLI (GitHub only for now).

    Returns None if the host is unsupported, the PR number cannot be parsed,
    or the CLI call fails.
    """
    if source.host != "github":
        return None

    match = _GH_PR_PATTERN.search(merge_title) or _PR_NUMBER_PATTERN.search(merge_title)
    if not match:
        return None

    pr_number = match.group(1)
    try:
        result = subprocess.run(
            ["gh", "pr", "view", pr_number, "--json", "title,body,labels"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
        if result.returncode != 0:
            logger.debug(
                "gh pr view failed for #%s: %s", pr_number, result.stderr.strip()
            )
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("gh pr view error: %s", exc)
        return None

    try:
        data = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        logger.debug("gh pr view returned invalid JSON for #%s", pr_number)
        return None

    raw_labels = data.get("labels") or []
    labels = tuple(
        _LABEL_SANITIZE.sub("", str(lbl["name"]))[:_MAX_LABEL_LEN]
        for lbl in raw_labels[:_MAX_LABELS]
        if isinstance(lbl, dict) and "name" in lbl
    )
    return PRMetadata(
        title=data.get("title") or merge_title,
        body=data.get("body") or "",
        labels=labels,
        source_tier="api",
    )


def _fetch_pr_metadata_from_commit(
    merge_sha: str,
    repo_path: Path,
) -> PRMetadata | None:
    """Extract metadata from the full commit message body."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%B", merge_sha],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_GIT_LOG_TIMEOUT,
        )
        if result.returncode != 0:
            return None
    except (subprocess.TimeoutExpired, OSError):
        return None

    text = result.stdout.strip()
    if not text:
        return None

    lines = text.split("\n", 1)
    title = lines[0].strip()
    body = lines[1].strip() if len(lines) > 1 else ""

    if not title:
        return None

    return PRMetadata(
        title=title,
        body=body,
        source_tier="commit_message",
    )


def resolve_pr_metadata(
    merge_sha: str,
    repo_path: Path,
    source: RepoSource,
    merge_title: str,
) -> PRMetadata:
    """Resolve PR metadata with three-tier fallback: API → commit message → bare."""
    # Tier 1: Host API (GitHub only; _fetch_pr_metadata_from_api returns None for other hosts)
    meta = _fetch_pr_metadata_from_api(repo_path, source, merge_title)
    if meta is not None:
        return meta

    # Tier 2: Commit message body
    meta = _fetch_pr_metadata_from_commit(merge_sha, repo_path)
    if meta is not None:
        return meta

    # Tier 3: Bare fallback
    short_sha = merge_sha[:8]
    return PRMetadata(
        title=merge_title or f"Merge commit {short_sha}",
        source_tier="bare",
    )


def _format_task_description(pr_meta: PRMetadata) -> str:
    """Build a task description from PR metadata.

    ZFC compliant: structural formatting only, no semantic interpretation.
    """
    parts = [pr_meta.title]
    if pr_meta.body:
        body = pr_meta.body.strip()
        if len(body) > _MAX_BODY_LENGTH:
            body = body[:_MAX_BODY_LENGTH] + "..."
        parts.append("")
        parts.append(body)
    if pr_meta.labels:
        parts.append("")
        parts.append(f"Labels: {', '.join(pr_meta.labels)}")
    return "\n".join(parts)


_MIN_MEANINGFUL_BODY_LEN = 20


def score_pr_quality(
    title: str,
    body: str,
    changed_files: list[str],
    test_files: list[str],
) -> float:
    """Score a PR's suitability as an eval task (0.0–1.0).

    Three structural signals, equally weighted:
    1. Issue/ticket reference in title or body (GitHub #N, JIRA PROJ-123)
    2. Meaningful description body (>20 chars)
    3. Test files that share a parent directory with non-test source files

    ZFC compliant: checks structural presence, not semantic quality.
    """
    score = 0.0
    text = f"{title}\n{body}"

    # Signal 1: Issue/ticket reference
    if _ISSUE_REF_PATTERN.search(text):
        score += 1 / 3

    # Signal 2: Non-empty body with meaningful length
    if len(body.strip()) >= _MIN_MEANINGFUL_BODY_LEN:
        score += 1 / 3

    # Signal 3: Test files target changed source files (name overlap)
    source_stems = {Path(f).stem for f in changed_files if f not in test_files}
    test_names = {Path(f).stem for f in test_files}
    if any(stem in tname for tname in test_names for stem in source_stems):
        score += 1 / 3

    return score


def extract_task_from_merge(
    merge_sha: str,
    repo_path: Path,
    changed_files: list[str] | None = None,
    source: RepoSource | None = None,
    merge_title: str = "",
) -> Task | None:
    """Extract an eval task from a merge commit.

    Returns None if the merge has no test files (can't verify the task).
    Pass *changed_files* to avoid a redundant git-diff call.
    """
    if changed_files is None:
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

    # Enrich description from PR metadata when source is available
    if source is not None:
        pr_meta = resolve_pr_metadata(merge_sha, repo_path, source, merge_title)
    else:
        pr_meta = PRMetadata(
            title=f"Reproduce changes from merge commit {short_sha}",
            source_tier="bare",
        )
    description = _format_task_description(pr_meta)

    metadata = TaskMetadata(
        name=f"merge-{short_sha}",
        difficulty=difficulty,
        description=description,
        language=language,
        category="sdlc",
    )

    # Build a verification command targeted to the detected test files and language
    test_command = _build_test_command(language, test_files)
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
    min_files: int = 0,
) -> list[Task]:
    """Mine eval tasks from a repository.

    1. Detect or use provided source
    2. List merged PRs
    3. Extract tasks from each merge (respecting min_files threshold)
    4. Sort by file count descending (larger changes first)
    5. Return up to *count* tasks
    """
    if source_hint != "auto":
        source = RepoSource(host=source_hint, owner="", repo=path.name, remote_url="")
    else:
        source = detect_source(path)
    logger.info("Detected source: %s (%s/%s)", source.host, source.owner, source.repo)

    # Fetch more merges than needed to account for filtering
    search_limit = count * 4 if min_files == 0 else count * 8
    prs = list_merged_prs(source, path, limit=search_limit)
    if not prs:
        logger.info("No merge commits found in %s", path)
        return []

    candidates: list[tuple[float, int, Task]] = []
    for pr in prs:
        changed_files = _get_changed_files(pr.merge_commit, path)
        if len(changed_files) < min_files:
            continue
        test_files = _find_test_files(changed_files)
        task = extract_task_from_merge(
            pr.merge_commit,
            path,
            changed_files=changed_files,
            source=source,
            merge_title=pr.title,
        )
        if task is not None:
            quality = score_pr_quality(
                title=pr.title,
                body=task.metadata.description,
                changed_files=changed_files,
                test_files=test_files,
            )
            candidates.append((quality, len(changed_files), task))

    # Sort by quality score descending, then file count descending
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    tasks = [task for _, _, task in candidates[:count]]

    logger.info(
        "Mined %d tasks from %d merge commits (min_files=%d)",
        len(tasks),
        len(prs),
        min_files,
    )
    return tasks
