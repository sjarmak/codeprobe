"""Task extraction from git history — mine eval tasks from merge commits."""

from __future__ import annotations

import json as _json
import logging
import re
import shlex
import subprocess
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from codeprobe.mining.sources import RepoSource, detect_source
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

logger = logging.getLogger(__name__)

_DIFF_STAT_TIMEOUT = 15
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_GIT_LOG_TIMEOUT = 15
_GH_TIMEOUT = 15
_MAX_BODY_LENGTH = 2000
_MAX_LABELS = 10
_MAX_LABEL_LEN = 64
_GH_PR_PATTERN = re.compile(r"[Mm]erge pull request #(\d+)")
_PR_NUMBER_PATTERN = re.compile(r"#(\d+)")
_LABEL_SANITIZE = re.compile(r"[^\w\s,./:-]")
_ISSUE_CLOSE_PATTERN = re.compile(
    r"(?:fix(?:es)?|close[sd]?|resolve[sd]?)\s+#(\d+)", re.IGNORECASE
)


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
    issue_title: str = ""
    issue_body: str = ""


def _list_merged_prs_gh(path: Path, limit: int) -> list[MergedPR] | None:
    """List merged PRs via GitHub CLI (``gh pr list``).

    Returns None if ``gh`` is unavailable or the command fails, so the caller
    can fall back to git-log.  Handles squash merges, rebase merges, and
    regular merge commits — anything GitHub marks as "merged".
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--limit",
                str(limit),
                "--json",
                "mergeCommit,title",
            ],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
        if result.returncode != 0:
            logger.debug("gh pr list failed: %s", result.stderr.strip())
            return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("gh not available: %s", exc)
        return None

    try:
        items = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        logger.debug("gh pr list returned invalid JSON")
        return None

    prs: list[MergedPR] = []
    for item in items:
        commit = item.get("mergeCommit") or {}
        sha = commit.get("oid", "")
        title = item.get("title", "")
        if sha:
            prs.append(MergedPR(sha=sha, title=title, merge_commit=sha))
    return prs if prs else None


def _list_merged_prs_git(path: Path, limit: int) -> list[MergedPR]:
    """List merged PRs from ``git log --merges`` (fallback).

    Only finds true merge commits — squash merges and rebase merges are
    invisible to this method.
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


def list_merged_prs(source: RepoSource, path: Path, limit: int = 20) -> list[MergedPR]:
    """List recently merged PRs.

    For GitHub repos, uses ``gh pr list`` which captures squash merges,
    rebase merges, and regular merge commits.  Falls back to
    ``git log --merges`` when ``gh`` is unavailable or for non-GitHub hosts.
    """
    if source.host == "github":
        gh_result = _list_merged_prs_gh(path, limit)
        if gh_result is not None:
            logger.info(
                "Listed %d merged PRs via GitHub API (includes squash merges)",
                len(gh_result),
            )
            return gh_result
        logger.info("GitHub API unavailable, falling back to git log --merges")

    return _list_merged_prs_git(path, limit)


def _validate_sha(sha: str) -> str:
    """Validate that *sha* looks like a hex commit hash.

    Prevents crafted revision strings from being interpreted as git flags.
    """
    if not _SHA_RE.match(sha):
        raise ValueError(f"Invalid merge SHA: {sha!r}")
    return sha


def _is_safe_relative_path(p: str) -> bool:
    """Return True if *p* is a safe repo-relative path (no traversal)."""
    try:
        parts = Path(p).parts
        return not Path(p).is_absolute() and ".." not in parts
    except (TypeError, ValueError):
        return False


def _get_changed_files(merge_sha: str, repo_path: Path) -> list[str]:
    """Get the list of changed files for a merge commit."""
    _validate_sha(merge_sha)
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

    return [
        f
        for f in result.stdout.strip().splitlines()
        if f.strip() and _is_safe_relative_path(f.strip())
    ]


def _get_deleted_dirs(merge_sha: str, repo_path: Path) -> set[str]:
    """Return directories that were entirely deleted in a merge commit.

    Uses ``git diff --diff-filter=D --name-status`` to find deleted files,
    then returns the set of parent directories where ALL files were deleted.
    """
    _validate_sha(merge_sha)
    try:
        result = subprocess.run(
            [
                "git",
                "diff",
                f"{merge_sha}^..{merge_sha}",
                "--diff-filter=D",
                "--name-only",
            ],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_DIFF_STAT_TIMEOUT,
        )
        if result.returncode != 0:
            return set()
    except (subprocess.TimeoutExpired, OSError):
        return set()

    deleted_files = [f for f in result.stdout.strip().splitlines() if f.strip()]
    if not deleted_files:
        return set()

    # Collect parent directories of deleted files
    return {str(Path(f).parent) for f in deleted_files}


def extract_subsystems(
    prs: list[MergedPR],
    repo_path: Path,
    depth: int = 2,
) -> dict[str, int]:
    """Extract top-level directory prefixes from merge commit changed files.

    Returns a dict mapping subsystem prefix to merge count (how many distinct
    merges touched files under that prefix), sorted by count descending.

    ZFC compliant: purely structural directory-name extraction.
    """
    from collections import Counter

    prefix_counts: Counter[str] = Counter()
    for pr in prs:
        changed = _get_changed_files(pr.merge_commit, repo_path)
        pr_prefixes: set[str] = set()
        for f in changed:
            parts = Path(f).parts
            if len(parts) < 2:
                # Root-level file (no directory) — skip
                continue
            n = min(depth, len(parts) - 1)
            prefix = "/".join(parts[:n]) + "/"
            pr_prefixes.add(prefix)
        prefix_counts.update(pr_prefixes)

    return dict(prefix_counts.most_common())


def _estimate_difficulty(changed_files: list[str]) -> str:
    """Estimate task difficulty based on the number of changed files."""
    count = len(changed_files)
    if count <= 3:
        return "easy"
    if count <= 10:
        return "medium"
    return "hard"


def _is_test_file(path: str) -> bool:
    """Return True if the path looks like a test or spec file."""
    lower = path.lower()
    return "test" in lower or "spec" in lower


def _find_test_files(changed_files: list[str]) -> list[str]:
    """Return files that look like tests from the changed file list."""
    return [f for f in changed_files if _is_test_file(f)]


def _extract_modified_symbols_from_diff(
    merge_sha: str,
    repo_path: Path,
    source_files: list[str],
) -> list[str]:
    """Extract public symbol names modified in a merge commit's diff.

    Runs ``git diff <sha>^..<sha> -- <file>`` for each source file and
    regex-matches definition lines (Python def/class, Go func, JS function).
    Purely structural — no semantic judgment.
    """
    _validate_sha(merge_sha)
    # Import shared symbol extractors from multi_repo
    from codeprobe.mining.multi_repo import _SYMBOL_EXTRACTORS

    symbols: list[str] = []
    seen: set[str] = set()

    for path in source_files:
        try:
            result = subprocess.run(
                ["git", "diff", f"{merge_sha}^..{merge_sha}", "--", path],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=_DIFF_STAT_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue

        for line in result.stdout.splitlines():
            if not (line.startswith("+") or line.startswith("-")):
                continue
            for regex in _SYMBOL_EXTRACTORS:
                match = regex.match(line)
                if match:
                    name = match.group(1)
                    if name.startswith("_"):
                        continue  # skip private symbols
                    if name not in seen:
                        seen.add(name)
                        symbols.append(name)
                    break

    return symbols


# ---------------------------------------------------------------------------
# Oracle ground truth generation (R16) + discrimination gate (R18)
# ---------------------------------------------------------------------------


def _build_oracle_ground_truth(
    merge_sha: str,
    repo_path: Path,
    changed_files: list[str],
) -> dict | None:
    """Build oracle ground truth from a PR's changed files.

    Filters out test files to produce the ``answer`` (source files only).
    Extracts modified symbols from the diff hunks for ``oracle_metadata``.

    Returns ``None`` when all changed files are test files (empty oracle).
    Uses ``answer`` field (not ``expected``) — required by
    ``ArtifactScorer._score_new_format()`` (scoring.py).
    """
    source_files = [
        f for f in changed_files if not _is_test_file(f) and _is_safe_relative_path(f)
    ]

    if not source_files:
        return None

    symbols = _extract_modified_symbols_from_diff(merge_sha, repo_path, source_files)

    return {
        "schema_version": 1,
        "answer_type": "file_list",
        "answer": source_files,
        "oracle_metadata": {
            "populated_by": "mining-phase-2",
            "merge_sha": merge_sha,
            "modified_symbols": symbols,
        },
    }


def _oracle_discrimination_passed(
    oracle: dict,
) -> tuple[bool, str]:
    """Check whether an auto-generated oracle is non-trivial (R18).

    Returns ``(passed, confidence)`` where *confidence* is ``"high"`` or
    ``"low"``.

    Discrimination criteria:
    - Empty answer → fails (``passed=False``, ``confidence="low"``)
    - Single file → passes with ``"low"`` confidence (trivially discoverable)
    - >80% of files in a single directory → ``"low"`` confidence
    - Otherwise → ``"high"`` confidence

    Low-confidence oracles still ship but are flagged so downstream consumers
    can filter or weight them.
    """
    answer = oracle.get("answer", [])
    if not answer:
        return False, "low"

    if len(answer) == 1:
        return True, "low"

    # Count files per parent directory
    dir_counts = Counter(str(Path(f).parent) for f in answer)
    max_count = dir_counts.most_common(1)[0][1]
    total = len(answer)

    if max_count / total >= 0.8:
        return True, "low"

    return True, "high"


def _discover_colocated_test_files(
    changed_files: list[str], repo_path: Path
) -> list[str]:
    """Find existing test files in the same packages as changed source files.

    Useful for Go repos where *_test.go files are rarely in the PR diff but
    exist in the same directory.  Also checks for Python test_*.py / *_test.py
    and JS/TS *.test.* / *.spec.* files.
    """
    if not repo_path.is_dir():
        return []

    # Collect unique directories of changed source files
    dirs: set[str] = set()
    for f in changed_files:
        parent = str(Path(f).parent)
        if parent != ".":
            dirs.add(parent)

    test_files: list[str] = []
    for d in sorted(dirs):
        abs_dir = repo_path / d
        if not abs_dir.is_dir():
            continue
        for child in abs_dir.iterdir():
            if not child.is_file():
                continue
            name = child.name.lower()
            if "test" in name or "spec" in name:
                test_files.append(str(child.relative_to(repo_path)))
    return test_files


_DEFAULT_TEST_COMMAND = "bash tests/test.sh"
_ISSUE_REF_PATTERN = re.compile(
    r"(?:#(\d+)|[A-Z][A-Z0-9]+-\d+)"  # GitHub #N or JIRA-style PROJ-123
)


def _build_test_command(
    language: str,
    test_files: list[str],
    repo_path: Path | None = None,
    deleted_dirs: set[str] | None = None,
) -> str:
    """Build a targeted test command from language and test file paths.

    Supports Python (pytest), Go (go test), and JS/TS (npm test with jest pattern).
    Falls back to the generic test.sh for unsupported languages or empty file lists.

    When *repo_path* is provided and exists on disk, validates that referenced
    paths exist in the target repo and drops any that don't.

    When *deleted_dirs* is provided and ALL test packages fall within deleted
    directories, generates a removal-verification command (checks dirs no longer
    exist) instead of a test command.
    """
    validate = repo_path is not None and repo_path.is_dir()

    if not test_files:
        return _DEFAULT_TEST_COMMAND

    if language == "python":
        if validate:
            test_files = [f for f in test_files if (repo_path / f).exists()]
        if not test_files:
            return _DEFAULT_TEST_COMMAND
        return f"pytest {' '.join(test_files)}"

    if language == "go":
        packages = sorted({str(Path(f).parent) for f in test_files})

        # Removal task: all test packages were deleted in this merge
        if deleted_dirs and all(
            any(pkg == d or pkg.startswith(d + "/") for d in deleted_dirs)
            for pkg in packages
        ):
            checks = " && ".join(
                f"test ! -d {shlex.quote('./' + pkg)}" for pkg in packages
            )
            return f"bash -c {shlex.quote(checks)}"

        if validate:
            packages = [p for p in packages if (repo_path / p).is_dir()]
        if not packages:
            return _DEFAULT_TEST_COMMAND
        go_paths = " ".join(f"./{pkg}/..." for pkg in packages)
        return f"go test {go_paths}"

    if language in ("javascript", "typescript"):
        pattern = Path(test_files[0]).name
        return f"npm test -- --testPathPattern={pattern}"

    return _DEFAULT_TEST_COMMAND


def _extract_issue_numbers(body: str) -> list[int]:
    """Parse PR body for linked issue references (Fixes #N, Closes #N, etc.).

    Returns deduplicated issue numbers ordered by first appearance.
    """
    seen: set[int] = set()
    result: list[int] = []
    for match in _ISSUE_CLOSE_PATTERN.finditer(body):
        num = int(match.group(1))
        if num not in seen:
            seen.add(num)
            result.append(num)
    return result


def _fetch_issue_body(repo_path: Path, issue_number: int) -> tuple[str, str] | None:
    """Fetch issue title and body via ``gh issue view``.

    Returns ``(title, body)`` or *None* on failure.
    """
    try:
        result = subprocess.run(
            ["gh", "issue", "view", str(issue_number), "--json", "title,body"],
            cwd=str(repo_path),
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT,
        )
        if result.returncode != 0:
            logger.debug(
                "gh issue view failed for #%d: %s",
                issue_number,
                result.stderr.strip(),
            )
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("gh issue view error: %s", exc)
        return None

    try:
        data = _json.loads(result.stdout)
    except _json.JSONDecodeError:
        logger.debug("gh issue view returned invalid JSON for #%d", issue_number)
        return None

    title = data.get("title") or ""
    body = data.get("body") or ""
    if not title:
        return None
    return (title, body)


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

    # Attempt to fetch linked issue content from PR body
    pr_body = data.get("body") or ""
    issue_title = ""
    issue_body = ""
    issue_numbers = _extract_issue_numbers(pr_body)
    if issue_numbers:
        issue_data = _fetch_issue_body(repo_path, issue_numbers[0])
        if issue_data is not None:
            issue_title, issue_body = issue_data

    return PRMetadata(
        title=data.get("title") or merge_title,
        body=pr_body,
        labels=labels,
        source_tier="api",
        issue_title=issue_title,
        issue_body=issue_body,
    )


def _fetch_pr_metadata_from_commit(
    merge_sha: str,
    repo_path: Path,
) -> PRMetadata | None:
    """Extract metadata from the full commit message body."""
    _validate_sha(merge_sha)
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
    *,
    has_linked_issue: bool = False,
) -> float:
    """Score a PR's suitability as an eval task (0.0–1.0).

    Four structural signals, equally weighted:
    1. Issue/ticket reference in title or body (GitHub #N, JIRA PROJ-123)
    2. Meaningful description body (>20 chars)
    3. Test files that share a parent directory with non-test source files
    4. Linked issue present (issue body fetched successfully)

    ZFC compliant: checks structural presence, not semantic quality.
    """
    score = 0.0

    # Signal 1: Issue/ticket reference in body (not title — GitHub merge
    # titles always contain "#N" which is noise, not a quality indicator)
    if body and _ISSUE_REF_PATTERN.search(body):
        score += 1 / 4

    # Signal 2: Non-empty body with meaningful length
    if len(body.strip()) >= _MIN_MEANINGFUL_BODY_LEN:
        score += 1 / 4

    # Signal 3: Test files target changed source files (name overlap)
    source_stems = {Path(f).stem for f in changed_files if f not in test_files}
    test_names = {Path(f).stem for f in test_files}
    if any(stem in tname for tname in test_names for stem in source_stems):
        score += 1 / 4

    # Signal 4: Linked issue present (problem description available)
    if has_linked_issue:
        score += 1 / 4

    return score


def extract_task_from_merge(
    merge_sha: str,
    repo_path: Path,
    changed_files: list[str] | None = None,
    source: RepoSource | None = None,
    merge_title: str = "",
) -> tuple[Task, PRMetadata] | None:
    """Extract an eval task from a merge commit.

    Returns None if the merge has no test files (can't verify the task).
    Pass *changed_files* to avoid a redundant git-diff call.
    Returns (task, pr_metadata) so callers can score against raw PR body.
    """
    if changed_files is None:
        changed_files = _get_changed_files(merge_sha, repo_path)
    if not changed_files:
        return None

    short_sha = merge_sha[:8]

    test_files = _find_test_files(changed_files)
    if not test_files:
        # For Go repos, test files often aren't in the PR diff — discover
        # existing *_test.go files in the same packages as changed .go files.
        test_files = _discover_colocated_test_files(changed_files, repo_path)
    if not test_files:
        logger.debug("Skipping %s: no test files in diff or repo packages", short_sha)
        return None
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

    # Hard gate: bare metadata means no useful instruction context — skip
    if pr_meta.source_tier == "bare":
        logger.debug(
            "Skipping %s: bare metadata (no PR body or commit message)", short_sha
        )
        return None

    # Detect deleted directories for removal-task verification
    deleted_dirs = _get_deleted_dirs(merge_sha, repo_path)

    # Build a verification command targeted to the detected test files and language.
    # Pass repo_path so missing packages (e.g. stripped vendor dirs) are filtered out.
    # Pass deleted_dirs so removal tasks verify non-existence instead of compilation.
    test_command = _build_test_command(language, test_files, repo_path, deleted_dirs)

    # Hard gate: stub test command means verification is meaningless — skip
    if test_command == _DEFAULT_TEST_COMMAND:
        logger.debug(
            "Skipping %s: stub test command (unsupported language or no test mapping)",
            short_sha,
        )
        return None

    description = _format_task_description(pr_meta)

    metadata = TaskMetadata(
        name=f"merge-{short_sha}",
        difficulty=difficulty,
        description=description,
        language=language,
        category="sdlc",
        issue_title=pr_meta.issue_title,
        issue_body=pr_meta.issue_body,
        ground_truth_commit=merge_sha,
    )

    verification = TaskVerification(
        type="test_script",
        command=test_command,
        reward_type="binary",
    )

    task = Task(
        id=short_sha,
        repo=repo_path.name,
        metadata=metadata,
        verification=verification,
    )
    return task, pr_meta


def _guess_language(extensions: list[str]) -> str:
    """Guess the primary language from file extensions."""
    from codeprobe.mining._lang import guess_language_from_extensions

    lang = guess_language_from_extensions(extensions)
    return "" if lang == "unknown" else lang


_MIN_QUALITY_SCORE = 2 / 4  # At least two quality signals required


@dataclass(frozen=True)
class MineResult:
    """Result of mine_tasks() including raw context for LLM generation."""

    tasks: list[Task]
    pr_bodies: dict[str, str]  # task.id → raw PR body
    changed_files_map: dict[str, list[str]]  # task.id → changed file paths
    min_files_used: int | None = None  # effective min_files after relaxation


def _collect_candidates(
    prs: list,
    path: Path,
    source: RepoSource,
    min_files: int,
    min_quality: float,
    subsystems: tuple[str, ...],
) -> tuple[list[tuple[float, int, Task]], dict[str, str], dict[str, list[str]]]:
    """Score and filter PRs into ranked candidates.

    Returns (candidates, pr_bodies, changed_files_map).
    """
    candidates: list[tuple[float, int, Task]] = []
    pr_bodies: dict[str, str] = {}
    changed_files_map: dict[str, list[str]] = {}

    # Rejection counters for diagnostics
    rejected_min_files = 0
    rejected_subsystem = 0
    rejected_extraction = 0
    rejected_quality = 0

    for pr in prs:
        changed_files = _get_changed_files(pr.merge_commit, path)
        if len(changed_files) < min_files:
            rejected_min_files += 1
            continue
        if subsystems and not any(
            f.startswith(prefix) for f in changed_files for prefix in subsystems
        ):
            rejected_subsystem += 1
            continue
        test_files = _find_test_files(changed_files)
        result = extract_task_from_merge(
            pr.merge_commit,
            path,
            changed_files=changed_files,
            source=source,
            merge_title=pr.title,
        )
        if result is None:
            rejected_extraction += 1
            continue
        task, pr_meta = result
        quality = score_pr_quality(
            title=pr_meta.title,
            body=pr_meta.body,
            changed_files=changed_files,
            test_files=test_files,
            has_linked_issue=bool(pr_meta.issue_title),
        )
        if quality < min_quality:
            logger.debug(
                "Skipping %s: quality %.2f < %.2f threshold",
                pr.merge_commit[:8],
                quality,
                min_quality,
            )
            rejected_quality += 1
            continue
        enriched_metadata = replace(task.metadata, quality_score=quality)
        task = replace(task, metadata=enriched_metadata)
        candidates.append((quality, len(changed_files), task))
        pr_bodies[task.id] = pr_meta.body
        changed_files_map[task.id] = changed_files

    total_rejected = (
        rejected_min_files + rejected_subsystem + rejected_extraction + rejected_quality
    )
    if total_rejected and not candidates:
        logger.info(
            "Rejection breakdown (%d PRs): min_files=%d, subsystem=%d, "
            "extraction=%d (no tests/bare metadata/stub cmd), quality=%d",
            len(prs),
            rejected_min_files,
            rejected_subsystem,
            rejected_extraction,
            rejected_quality,
        )

    return candidates, pr_bodies, changed_files_map


# Thresholds to try when min_files filters out all candidates.
# Each step halves the previous value (rounded down), bottoming out at 1.
_MIN_FILES_RELAXATION = (3, 1)


def mine_tasks(
    path: Path,
    count: int = 5,
    source_hint: str = "auto",
    min_files: int = 0,
    min_quality: float = _MIN_QUALITY_SCORE,
    subsystems: tuple[str, ...] = (),
) -> MineResult:
    """Mine eval tasks from a repository.

    1. Detect or use provided source
    2. List merged PRs
    3. Filter by subsystem prefixes (if provided)
    4. Extract tasks from each merge (respecting min_files threshold)
    5. Filter out tasks below *min_quality* score
    6. Sort by quality descending, then file count descending
    7. If no candidates survive and min_files > 1, retry with relaxed thresholds
    8. Return MineResult with tasks and raw context for LLM instruction generation
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
        return MineResult(tasks=[], pr_bodies={}, changed_files_map={})

    candidates, pr_bodies, changed_files_map = _collect_candidates(
        prs,
        path,
        source,
        min_files,
        min_quality,
        subsystems,
    )

    # Relax min_files if the threshold filtered out everything
    if not candidates and min_files > 1:
        for relaxed in _MIN_FILES_RELAXATION:
            if relaxed >= min_files:
                continue
            logger.warning(
                "No candidates with min_files=%d; relaxing to min_files=%d",
                min_files,
                relaxed,
            )
            candidates, pr_bodies, changed_files_map = _collect_candidates(
                prs,
                path,
                source,
                relaxed,
                min_quality,
                subsystems,
            )
            if candidates:
                min_files = relaxed
                break

    # Sort by quality score descending, then file count descending
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    tasks = [task for _, _, task in candidates[:count]]

    # Prune context maps to only include selected tasks
    selected_ids = {t.id for t in tasks}
    pr_bodies = {k: v for k, v in pr_bodies.items() if k in selected_ids}
    changed_files_map = {
        k: v for k, v in changed_files_map.items() if k in selected_ids
    }

    logger.info(
        "Mined %d tasks from %d merge commits (min_files=%d)",
        len(tasks),
        len(prs),
        min_files,
    )
    return MineResult(
        tasks=tasks,
        pr_bodies=pr_bodies,
        changed_files_map=changed_files_map,
        min_files_used=min_files,
    )


# ---------------------------------------------------------------------------
# LLM instruction generation
# ---------------------------------------------------------------------------

_ENRICHMENT_THRESHOLD = 0.5

_INSTRUCTION_PROMPT_TEMPLATE = """\
You are an eval-task writer for an AI coding agent benchmark. Given raw PR \
metadata from a merged pull request, write a clear task instruction that tells \
an agent WHAT to implement without revealing HOW (the solution).

## Raw PR Metadata
Title: {title}
Body:
{body}

Issue title: {issue_title}
Issue body: {issue_body}

Labels: {labels}
Language: {language}
Changed files: {changed_files}

## Instructions
Produce a JSON object with exactly these keys:
- "heading": A short descriptive title for the task (not the raw PR title).
- "problem": A clear 2-4 sentence problem description. Explain what is broken \
or what feature is needed and why. Do NOT describe the solution or mention \
specific code changes from the PR. If there is issue context, use it — it \
describes the problem better than the PR body.
- "requirements": A bullet list (as a single string with newlines) of what the \
agent must accomplish. Focus on observable behavior and acceptance criteria, \
not implementation details.
- "difficulty": One of "easy", "medium", or "hard".

Strip any PR template boilerplate (e.g., "What type of PR is this?", \
checklists, release notes, bot labels). Focus on the actual problem.

Respond ONLY with the JSON object, no markdown fences.
"""


def _build_instruction_prompt(
    task: Task,
    pr_body: str = "",
    changed_files: list[str] | None = None,
) -> str:
    """Build the LLM prompt for generating task instructions."""
    return _INSTRUCTION_PROMPT_TEMPLATE.format(
        title=task.metadata.name,
        body=pr_body or task.metadata.description,
        issue_title=task.metadata.issue_title or "(none)",
        issue_body=task.metadata.issue_body or "(none)",
        labels="(none)",
        language=task.metadata.language or "unknown",
        changed_files=", ".join(changed_files[:20]) if changed_files else "(unknown)",
    )


def generate_instruction(
    task: Task,
    pr_body: str = "",
    changed_files: list[str] | None = None,
) -> Task:
    """Generate instruction content for a task via LLM.

    Replaces the raw PR description with an LLM-generated problem statement
    and requirements. Sets enrichment_source='llm' in metadata.

    On LLM failure, returns the task unchanged (logs warning).
    """
    from codeprobe.core.llm import LLMError, LLMRequest, call_claude

    prompt = _build_instruction_prompt(task, pr_body, changed_files)
    try:
        response = call_claude(
            LLMRequest(prompt=prompt, model="haiku", timeout_seconds=30)
        )
    except LLMError as exc:
        logger.warning("LLM instruction generation failed for %s: %s", task.id, exc)
        return task

    text = response.text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        data = _json.loads(text)
    except _json.JSONDecodeError:
        logger.warning(
            "LLM returned invalid JSON for %s: %.100s", task.id, response.text
        )
        return task

    heading = data.get("heading", "")
    problem = data.get("problem", "")
    requirements = data.get("requirements", "")
    difficulty = data.get("difficulty", task.metadata.difficulty)

    if difficulty not in ("easy", "medium", "hard"):
        difficulty = task.metadata.difficulty

    if not problem:
        # LLM returned empty problem — keep original
        return task

    new_metadata = replace(
        task.metadata,
        description=task.metadata.description,  # preserve raw for metadata.json
        difficulty=difficulty,
        enrichment_source="llm",
        issue_title=heading or task.metadata.issue_title,
        issue_body=problem
        + ("\n\n## Requirements\n\n" + requirements if requirements else ""),
    )
    return replace(task, metadata=new_metadata)


def generate_instructions(
    tasks: list[Task],
    pr_bodies: dict[str, str] | None = None,
    changed_files_map: dict[str, list[str]] | None = None,
) -> list[Task]:
    """Generate LLM instructions for all tasks.

    *pr_bodies* maps task.id → raw PR body text.
    *changed_files_map* maps task.id → list of changed file paths.
    """
    pr_bodies = pr_bodies or {}
    changed_files_map = changed_files_map or {}
    result: list[Task] = []
    for task in tasks:
        logger.info("Generating instruction for %s via LLM", task.id)
        result.append(
            generate_instruction(
                task,
                pr_body=pr_bodies.get(task.id, ""),
                changed_files=changed_files_map.get(task.id),
            )
        )
    return result


# Legacy enrichment — kept for backward compatibility with --enrich flag


def enrich_task(task: Task) -> Task:
    """Enrich a single task via LLM (legacy wrapper around generate_instruction)."""
    return generate_instruction(task)


def enrich_tasks(tasks: list[Task]) -> list[Task]:
    """Enrich tasks with quality_score below the threshold via LLM.

    Tasks at or above the threshold are passed through unchanged.
    """
    result: list[Task] = []
    for task in tasks:
        if task.metadata.quality_score < _ENRICHMENT_THRESHOLD:
            logger.info(
                "Enriching task %s (quality=%.2f)",
                task.id,
                task.metadata.quality_score,
            )
            result.append(enrich_task(task))
        else:
            result.append(task)
    return result
