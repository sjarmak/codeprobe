"""Codebase heuristics — gather metrics and score benchmarking potential."""

from __future__ import annotations

import json
import logging
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
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

# Recursive variants for repos with nested test layouts (e.g. numpy/_core/tests/).
_RECURSIVE_TEST_DIR_GLOBS: list[str] = [
    "**/tests/**",
    "**/test/**",
    "**/spec/**",
    "**/__tests__/**",
]

_RECURSIVE_TEST_FILE_GLOBS: list[str] = [f"**/{p}" for p in _TEST_GLOBS]

# ---------------------------------------------------------------------------
# Fixed rubric — model scores against these, doesn't invent them
# ---------------------------------------------------------------------------

RUBRIC_V1: tuple[str, ...] = (
    "task_richness",
    "test_coverage",
    "complexity",
    "activity",
    "documentation",
    "ci_maturity",
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


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
    has_docs: bool


@dataclass(frozen=True)
class DimensionScore:
    """A single scoring dimension with reasoning."""

    name: str
    score: float
    reasoning: str


@dataclass(frozen=True)
class AssessmentScore:
    """Benchmarking potential score with per-dimension breakdown."""

    overall: float
    recommendation: str
    dimensions: tuple[DimensionScore, ...]
    scoring_method: str  # "model" or "heuristic"
    model_used: str | None = None
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
            logger.debug(
                "git %s exited %d: %s",
                " ".join(args),
                result.returncode,
                result.stderr.strip(),
            )
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
    """Check whether the repo appears to contain tests.

    Checks top-level test directories first, then falls back to recursive
    git ls-files glob patterns to catch repos with nested test layouts
    (e.g. numpy/_core/tests/, numpy/tests/).
    """
    # Fast path: top-level test directories
    for d in _TEST_DIRS:
        if (repo_path / d).is_dir():
            return True
    # Single git ls-files call with all patterns (top-level + recursive)
    all_patterns = _TEST_GLOBS + _RECURSIVE_TEST_DIR_GLOBS + _RECURSIVE_TEST_FILE_GLOBS
    out = _run_git(["ls-files", "--", *all_patterns], cwd=repo_path)
    return bool(out)


def _has_ci(repo_path: Path) -> bool:
    """Check whether the repo has CI configuration."""
    for ci_path in _CI_PATHS:
        full = repo_path / ci_path
        if full.exists():
            return True
    return False


def _has_docs(repo_path: Path) -> bool:
    """Check whether the repo has documentation files."""
    for name in ("README.md", "README.rst", "README.txt", "README"):
        if (repo_path / name).is_file():
            return True
    if (repo_path / "docs").is_dir():
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


def _extract_json(text: str) -> str:
    """Extract JSON from model response, stripping markdown fences if present."""
    stripped = text.strip()
    # Strip ```json ... ``` or ``` ... ``` fences
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Remove first line (```json or ```) and last line (```)
        if lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        stripped = "\n".join(lines).strip()
    return stripped


def _heuristics_to_dict(h: RepoHeuristics) -> dict[str, object]:
    """Serialize RepoHeuristics to a plain dict for JSON encoding."""
    return {
        "total_commits": h.total_commits,
        "merge_commits": h.merge_commits,
        "contributors": h.contributors,
        "has_ci": h.has_ci,
        "has_tests": h.has_tests,
        "test_frameworks": list(h.test_frameworks),
        "primary_languages": list(h.primary_languages),
        "total_files": h.total_files,
        "repo_age_days": h.repo_age_days,
        "recent_activity": h.recent_activity,
        "has_docs": h.has_docs,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def gather_heuristics(repo_path: Path) -> RepoHeuristics:
    """Gather raw heuristics about a repository.

    Uses git and filesystem inspection to collect metrics about commit
    history, CI presence, test coverage, languages, and activity.
    """
    total_commits_str = _run_git(["rev-list", "--count", "HEAD"], cwd=repo_path)
    merge_commits_str = _run_git(
        ["rev-list", "--merges", "--count", "HEAD"], cwd=repo_path
    )
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
        has_docs=_has_docs(repo_path),
    )


def score_repo_heuristic(heuristics: RepoHeuristics) -> AssessmentScore:
    """Score a repo's benchmarking potential from heuristics.

    Returns an ``AssessmentScore`` with per-dimension breakdown using
    the fixed ``RUBRIC_V1`` dimensions.  This is the fallback path when
    the Claude CLI is unavailable or the model call fails.
    """
    # --- task_richness ---
    mc = heuristics.merge_commits
    if mc >= 50:
        tr_score, tr_reason = 1.0, f"{mc} merge commits — rich task history"
    elif mc >= 20:
        tr_score, tr_reason = 0.7, f"{mc} merge commits — moderate task variety"
    elif mc >= 5:
        tr_score, tr_reason = 0.4, f"{mc} merge commits — limited tasks"
    else:
        tr_score, tr_reason = 0.1, f"Only {mc} merge commits — very few mineable tasks"

    # --- test_coverage ---
    has_tests = heuristics.has_tests
    has_ci = heuristics.has_ci
    has_fw = len(heuristics.test_frameworks) > 0
    if has_tests and has_ci and has_fw:
        tc_score, tc_reason = (
            1.0,
            f"Tests + CI + framework ({', '.join(heuristics.test_frameworks)})",
        )
    elif has_tests and (has_ci or has_fw):
        tc_score, tc_reason = 0.7, "Tests present with partial CI/framework support"
    elif has_tests:
        tc_score, tc_reason = 0.4, "Tests present but no CI or recognized framework"
    else:
        tc_score, tc_reason = 0.0, "No tests detected"

    # --- complexity ---
    tf = heuristics.total_files
    tc = heuristics.total_commits
    if tf >= 100 and tc >= 500:
        cx_score, cx_reason = 1.0, f"{tf} files, {tc} commits — substantial codebase"
    elif tf >= 50 and tc >= 100:
        cx_score, cx_reason = 0.7, f"{tf} files, {tc} commits — moderate complexity"
    elif tf >= 20 and tc >= 50:
        cx_score, cx_reason = 0.4, f"{tf} files, {tc} commits — small codebase"
    else:
        cx_score, cx_reason = 0.2, f"{tf} files, {tc} commits — minimal codebase"

    # --- activity ---
    ra = heuristics.recent_activity
    co = heuristics.contributors
    if ra and co >= 3:
        ac_score, ac_reason = 1.0, f"Active ({co} contributors, recent commits)"
    elif ra or co >= 2:
        ac_score, ac_reason = 0.6, f"Moderate activity ({co} contributors)"
    else:
        ac_score, ac_reason = 0.2, "Low activity — single contributor or stale"

    # --- documentation ---
    if heuristics.has_docs and has_ci:
        doc_score, doc_reason = 0.8, "Documentation and CI present"
    elif heuristics.has_docs:
        doc_score, doc_reason = 0.5, "README or docs/ directory present"
    else:
        doc_score, doc_reason = 0.1, "No documentation detected"

    # --- ci_maturity ---
    if has_ci and has_fw:
        ci_score, ci_reason = 1.0, "CI pipeline with test framework integration"
    elif has_ci:
        ci_score, ci_reason = 0.6, "CI present but no recognized test framework"
    else:
        ci_score, ci_reason = 0.0, "No CI configuration detected"

    dimensions = (
        DimensionScore(name="task_richness", score=tr_score, reasoning=tr_reason),
        DimensionScore(name="test_coverage", score=tc_score, reasoning=tc_reason),
        DimensionScore(name="complexity", score=cx_score, reasoning=cx_reason),
        DimensionScore(name="activity", score=ac_score, reasoning=ac_reason),
        DimensionScore(name="documentation", score=doc_score, reasoning=doc_reason),
        DimensionScore(name="ci_maturity", score=ci_score, reasoning=ci_reason),
    )

    # Weighted average — ci_maturity is a weak signal because CI configs are
    # often absent in shallow clones / Sourcegraph views, and codeprobe
    # validates via mined test.sh scripts, not CI pipelines.
    _WEIGHTS: dict[str, float] = {
        "task_richness": 0.25,
        "test_coverage": 0.25,
        "complexity": 0.20,
        "activity": 0.15,
        "documentation": 0.10,
        "ci_maturity": 0.05,
    }
    overall = sum(d.score * _WEIGHTS[d.name] for d in dimensions)

    if overall >= 0.7:
        recommendation = "Excellent benchmarking candidate — rich history with tests"
    elif overall >= 0.5:
        recommendation = (
            "Good candidate — may need more merge history for diverse tasks"
        )
    elif overall >= 0.3:
        recommendation = (
            "Fair candidate — limited test coverage may reduce task quality"
        )
    else:
        recommendation = "Poor candidate — consider a repo with more history and tests"

    return AssessmentScore(
        overall=overall,
        recommendation=recommendation,
        dimensions=dimensions,
        scoring_method="heuristic",
        model_used=None,
        details=_heuristics_to_dict(heuristics),
    )


# Backward-compatible alias.
score_repo = score_repo_heuristic


def _parse_model_assessment(
    parsed: dict[str, object],
    model_used: str | None,
    details: dict[str, object],
) -> AssessmentScore:
    """Validate and convert model JSON response to AssessmentScore."""
    from codeprobe.core.llm import LLMParseError

    if not isinstance(parsed.get("dimensions"), list):
        raise LLMParseError("Model response missing 'dimensions' list")

    dim_by_name: dict[str, DimensionScore] = {}
    for item in parsed["dimensions"]:
        if not isinstance(item, dict):
            raise LLMParseError(f"Dimension entry is not an object: {item!r}")
        name = item.get("name", "")
        if name not in RUBRIC_V1:
            continue  # ignore extra dimensions
        if name in dim_by_name:
            raise LLMParseError(f"Duplicate dimension in model response: {name!r}")
        score_val = float(item.get("score", 0))
        score_val = max(0.0, min(1.0, score_val))
        reasoning = str(item.get("reasoning", ""))
        dim_by_name[name] = DimensionScore(
            name=name, score=score_val, reasoning=reasoning
        )

    missing = set(RUBRIC_V1) - set(dim_by_name)
    if missing:
        raise LLMParseError(
            f"Model response missing dimensions: {', '.join(sorted(missing))}"
        )

    dimensions = tuple(dim_by_name[name] for name in RUBRIC_V1)

    overall_raw = parsed.get("overall")
    if isinstance(overall_raw, (int, float)):
        overall = max(0.0, min(1.0, float(overall_raw)))
    else:
        overall = sum(d.score for d in dimensions) / len(dimensions)

    recommendation = str(parsed.get("recommendation", ""))
    if not recommendation:
        recommendation = "Model did not provide a recommendation"

    return AssessmentScore(
        overall=overall,
        recommendation=recommendation,
        dimensions=dimensions,
        scoring_method="model",
        model_used=model_used,
        details=details,
    )


def score_repo_with_model(heuristics: RepoHeuristics) -> AssessmentScore:
    """Score repo by sending raw stats to Claude for judgment."""
    from codeprobe.core.llm import LLMRequest, call_claude

    details = _heuristics_to_dict(heuristics)
    stats_json = json.dumps(details, indent=2)

    rubric_list = ", ".join(RUBRIC_V1)
    prompt = (
        "You are evaluating a code repository's suitability for AI agent benchmarking.\n\n"
        f"Here are the raw repository statistics:\n{stats_json}\n\n"
        f"Score this repository on each of these dimensions (0.0 to 1.0):\n{rubric_list}\n\n"
        "Weighting guidance for the overall score: task_richness and test_coverage "
        "are the most important (~25% each), followed by complexity (~20%), "
        "activity (~15%), documentation (~10%). ci_maturity should be a minor "
        "signal (~5%) because CI configs are often absent in cloned repos and "
        "codeprobe validates via mined test scripts, not CI pipelines.\n\n"
        "Respond with ONLY valid JSON matching this exact schema:\n"
        "{\n"
        '  "overall": <float 0.0-1.0>,\n'
        '  "recommendation": "<1-2 sentence recommendation>",\n'
        '  "dimensions": [\n'
        '    {"name": "<dimension>", "score": <float>, "reasoning": "<1 sentence>"}\n'
        "  ]\n"
        "}\n\n"
        "Every dimension from the list must appear exactly once in dimensions."
    )

    request = LLMRequest(prompt=prompt, model="haiku", timeout_seconds=60)
    response = call_claude(request)
    text = _extract_json(response.text)

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        from codeprobe.core.llm import LLMParseError

        raise LLMParseError(f"Model returned invalid JSON: {exc}") from exc

    return _parse_model_assessment(parsed, model_used=response.model, details=details)


def assess_repo(repo_path: Path) -> AssessmentScore:
    """Main entry: gather heuristics and score the repo.

    Attempts model-based scoring via Claude CLI.  Falls back to heuristic
    scoring when the CLI is unavailable or the model call fails.
    """
    from codeprobe.core.llm import LLMError, claude_available

    heuristics = gather_heuristics(repo_path)

    if claude_available():
        try:
            return score_repo_with_model(heuristics)
        except LLMError as exc:
            logger.warning("Model scoring failed (%s), falling back to heuristics", exc)
    else:
        logger.info("Claude CLI not found; using heuristic scoring")

    return score_repo_heuristic(heuristics)
