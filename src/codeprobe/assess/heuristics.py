"""Codebase heuristics — gather metrics and score benchmarking potential."""

from __future__ import annotations

import json
import logging
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Extension-to-language mapping for the most common languages.
_EXT_LANG: dict[str, str] = {
    ".py": "Python",
    ".js": "JavaScript",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".java": "Java",
    ".kt": "Kotlin",
    ".rb": "Ruby",
    ".cpp": "C++",
    ".c": "C",
    ".h": "C",
    ".hpp": "C++",
    ".cs": "C#",
    ".swift": "Swift",
    ".php": "PHP",
    ".scala": "Scala",
    ".sh": "Shell",
    ".bash": "Shell",
    ".lua": "Lua",
    ".r": "R",
    ".pl": "Perl",
    ".pm": "Perl",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".zig": "Zig",
}

# CI configuration paths to check.
_CI_PATHS: list[str] = [
    ".github/workflows",
    ".gitlab-ci.yml",
    "Jenkinsfile",
    ".circleci",
    ".travis.yml",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
]

# Test directory / file patterns.
_TEST_DIRS: list[str] = ["tests", "test", "__tests__", "spec"]

_TEST_GLOBS: list[str] = [
    "*_test.go",
    "*_test.py",
    "test_*.py",
    "*.test.ts",
    "*.test.js",
    "*.test.tsx",
    "*.test.jsx",
    "*.spec.ts",
    "*.spec.js",
]


@dataclass(frozen=True)
class RepoHeuristics:
    """Raw metrics about a repository."""

    total_commits: int
    merge_commits: int
    contributors: int
    has_ci: bool
    has_tests: bool
    test_frameworks: tuple[str, ...]
    primary_languages: tuple[str, ...]
    total_files: int
    repo_age_days: int
    recent_activity: bool


@dataclass(frozen=True)
class AssessmentScore:
    """Benchmarking potential score with breakdown."""

    overall: float
    task_richness: float
    test_coverage: float
    complexity: float
    activity: float
    recommendation: str
    details: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> str:
    """Run a git command and return stripped stdout, or empty string on error."""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.debug("git %s exited %d: %s", " ".join(args), result.returncode, result.stderr.strip())
            return ""
        return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("git %s failed: %s", " ".join(args), exc)
        return ""


def _count_lines(text: str) -> int:
    """Return the number of non-empty lines in *text*."""
    if not text:
        return 0
    return len([line for line in text.splitlines() if line.strip()])


def _detect_test_frameworks(repo_path: Path) -> list[str]:
    """Detect test frameworks from config files."""
    found: set[str] = set()

    # pytest — pyproject.toml or setup.cfg
    for config_file in (repo_path / "pyproject.toml", repo_path / "setup.cfg"):
        if config_file.is_file():
            try:
                content = config_file.read_text()
                if "pytest" in content:
                    found.add("pytest")
            except OSError:
                pass

    # jest / mocha / vitest — package.json
    package_json = repo_path / "package.json"
    if package_json.is_file():
        try:
            data = json.loads(package_json.read_text())
            all_deps = {
                *data.get("dependencies", {}).keys(),
                *data.get("devDependencies", {}).keys(),
            }
            js_frameworks = {"jest", "mocha", "vitest", "ava", "jasmine"}
            found.update(all_deps & js_frameworks)
            # Also check scripts for jest/vitest
            script_values = " ".join(
                v for v in data.get("scripts", {}).values() if isinstance(v, str)
            )
            for fw in ("jest", "vitest"):
                if fw in script_values:
                    found.add(fw)
        except (OSError, json.JSONDecodeError):
            pass

    # go test — any *_test.go file
    if _run_git(["ls-files", "--", "*_test.go"], cwd=repo_path):
        found.add("go test")

    return sorted(found)


def _detect_primary_languages(file_list: str) -> list[str]:
    """Return top 3 languages by file count from git ls-files output."""
    counter: Counter[str] = Counter()
    for line in file_list.splitlines():
        path = Path(line.strip())
        ext = path.suffix.lower()
        lang = _EXT_LANG.get(ext)
        if lang:
            counter[lang] += 1
    return [lang for lang, _ in counter.most_common(3)]


def _has_tests(repo_path: Path) -> bool:
    """Check whether the repo appears to contain tests."""
    for d in _TEST_DIRS:
        if (repo_path / d).is_dir():
            return True
    # Check for test files via git ls-files
    for pattern in _TEST_GLOBS:
        out = _run_git(["ls-files", "--", pattern], cwd=repo_path)
        if out:
            return True
    return False


def _has_ci(repo_path: Path) -> bool:
    """Check whether the repo has CI configuration."""
    for ci_path in _CI_PATHS:
        full = repo_path / ci_path
        if full.exists():
            return True
    return False


def _repo_age_days(cwd: Path) -> int:
    """Return the number of days between the first and last commit."""
    first = _run_git(["log", "--reverse", "--format=%aI", "--max-count=1"], cwd=cwd)
    last = _run_git(["log", "--format=%aI", "--max-count=1"], cwd=cwd)
    if not first or not last:
        return 0
    try:
        first_dt = datetime.fromisoformat(first)
        last_dt = datetime.fromisoformat(last)
        return max(0, (last_dt - first_dt).days)
    except ValueError:
        return 0


def _recent_activity(cwd: Path) -> bool:
    """Return True if there are commits in the last 30 days."""
    out = _run_git(["log", "--since=30.days", "--oneline", "--max-count=1"], cwd=cwd)
    return bool(out)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def gather_heuristics(repo_path: Path) -> RepoHeuristics:
    """Gather raw heuristics about a repository.

    Uses git and filesystem inspection to collect metrics about commit
    history, CI presence, test coverage, languages, and activity.
    """
    total_commits_str = _run_git(["rev-list", "--count", "HEAD"], cwd=repo_path)
    merge_commits_str = _run_git(["rev-list", "--merges", "--count", "HEAD"], cwd=repo_path)
    contributors_str = _run_git(["shortlog", "-sn", "HEAD"], cwd=repo_path)
    file_list = _run_git(["ls-files"], cwd=repo_path)

    total_commits = int(total_commits_str) if total_commits_str.isdigit() else 0
    merge_commits = int(merge_commits_str) if merge_commits_str.isdigit() else 0
    contributors = _count_lines(contributors_str)
    total_files = _count_lines(file_list)

    return RepoHeuristics(
        total_commits=total_commits,
        merge_commits=merge_commits,
        contributors=contributors,
        has_ci=_has_ci(repo_path),
        has_tests=_has_tests(repo_path),
        test_frameworks=tuple(_detect_test_frameworks(repo_path)),
        primary_languages=tuple(_detect_primary_languages(file_list)),
        total_files=total_files,
        repo_age_days=_repo_age_days(repo_path),
        recent_activity=_recent_activity(repo_path),
    )


def score_repo(heuristics: RepoHeuristics) -> AssessmentScore:
    """Score a repo's benchmarking potential from heuristics.

    Returns an ``AssessmentScore`` with per-signal breakdown and a
    weighted overall score between 0.0 and 1.0.
    """
    # --- task_richness (weight 0.35) ---
    mc = heuristics.merge_commits
    if mc >= 50:
        task_richness = 1.0
    elif mc >= 20:
        task_richness = 0.7
    elif mc >= 5:
        task_richness = 0.4
    else:
        task_richness = 0.1

    # --- test_coverage (weight 0.30) ---
    has_tests = heuristics.has_tests
    has_ci = heuristics.has_ci
    has_fw = len(heuristics.test_frameworks) > 0
    if has_tests and has_ci and has_fw:
        test_coverage = 1.0
    elif has_tests and (has_ci or has_fw):
        test_coverage = 0.7
    elif has_tests:
        test_coverage = 0.4
    else:
        test_coverage = 0.0

    # --- complexity (weight 0.20) ---
    tf = heuristics.total_files
    tc = heuristics.total_commits
    if tf >= 100 and tc >= 500:
        complexity = 1.0
    elif tf >= 50 and tc >= 100:
        complexity = 0.7
    elif tf >= 20 and tc >= 50:
        complexity = 0.4
    else:
        complexity = 0.2

    # --- activity (weight 0.15) ---
    ra = heuristics.recent_activity
    co = heuristics.contributors
    if ra and co >= 3:
        activity = 1.0
    elif ra or co >= 2:
        activity = 0.6
    else:
        activity = 0.2

    # --- overall ---
    overall = (
        0.35 * task_richness
        + 0.30 * test_coverage
        + 0.20 * complexity
        + 0.15 * activity
    )

    # --- recommendation ---
    if overall >= 0.7:
        recommendation = "Excellent benchmarking candidate — rich history with tests"
    elif overall >= 0.5:
        recommendation = "Good candidate — may need more merge history for diverse tasks"
    elif overall >= 0.3:
        recommendation = "Fair candidate — limited test coverage may reduce task quality"
    else:
        recommendation = "Poor candidate — consider a repo with more history and tests"

    return AssessmentScore(
        overall=overall,
        task_richness=task_richness,
        test_coverage=test_coverage,
        complexity=complexity,
        activity=activity,
        recommendation=recommendation,
        details={
            "merge_commits": heuristics.merge_commits,
            "total_commits": heuristics.total_commits,
            "total_files": heuristics.total_files,
            "contributors": heuristics.contributors,
            "has_ci": heuristics.has_ci,
            "has_tests": heuristics.has_tests,
            "test_frameworks": heuristics.test_frameworks,
            "primary_languages": heuristics.primary_languages,
            "repo_age_days": heuristics.repo_age_days,
            "recent_activity": heuristics.recent_activity,
        },
    )


def assess_repo(repo_path: Path) -> AssessmentScore:
    """Main entry: gather heuristics and score the repo."""
    heuristics = gather_heuristics(repo_path)
    return score_repo(heuristics)
