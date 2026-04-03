"""Structural scanner for org-scale task mining.

Scans codebases for structural patterns (deprecated APIs, security configs,
import dependencies) using globs + regex. ZFC compliant: mechanism only.
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections import Counter
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from codeprobe.mining.org_scale_families import FAMILIES, TaskFamily

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 15
_MAX_LINE_LEN = 500


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatternHit:
    """A single pattern match in a file."""

    file_path: str
    line_number: int
    matched_text: str
    pattern_used: str


@dataclass(frozen=True)
class FamilyScanResult:
    """Results of scanning a repo for one task family."""

    family: TaskFamily
    hits: tuple[PatternHit, ...]
    repo_path: Path
    commit_sha: str
    matched_files: frozenset[str]


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def get_head_sha(repo_path: Path) -> str:
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


def get_tracked_files(repo_path: Path) -> frozenset[str]:
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
# Glob matching
# ---------------------------------------------------------------------------


def matches_glob(file_path: str, glob_pattern: str) -> bool:
    """Check if a file path matches a glob pattern."""
    if glob_pattern.startswith("**/"):
        pattern = glob_pattern[3:]
        return fnmatch(file_path, pattern) or any(
            fnmatch(part, pattern.split("/")[-1]) for part in file_path.split("/")
        )
    return fnmatch(file_path, glob_pattern)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


_MAX_FILE_SIZE = 512_000  # Skip files larger than 512KB (generated/bundled)


def scan_repo_for_family(
    repo_path: Path,
    family: TaskFamily,
    *,
    max_files: int = 50_000,
    tracked_files: frozenset[str] | None = None,
    commit_sha: str = "",
) -> FamilyScanResult:
    """Scan a repo for pattern matches belonging to a task family."""
    if not commit_sha:
        commit_sha = get_head_sha(repo_path)
    if tracked_files is None:
        tracked_files = get_tracked_files(repo_path)

    candidate_files = _filter_by_suffix(tracked_files, family.glob_patterns, max_files)
    compiled_patterns = _compile_patterns(family)
    hits, matched_files = _scan_files(
        repo_path, candidate_files, compiled_patterns, family.max_hits
    )

    return FamilyScanResult(
        family=family,
        hits=tuple(hits),
        repo_path=repo_path,
        commit_sha=commit_sha,
        matched_files=frozenset(matched_files),
    )


def _filter_by_suffix(
    tracked_files: frozenset[str],
    glob_patterns: tuple[str, ...],
    max_files: int,
) -> list[str]:
    """Filter tracked files by extension suffix, capped at max_files.

    Extracts suffixes from glob patterns (e.g., ``**/*.go`` → ``.go``) for
    O(1) set-lookup filtering instead of O(patterns) fnmatch per file.
    Falls back to fnmatch for non-standard patterns.
    """
    # Extract simple suffixes from **/*.ext patterns
    suffixes: set[str] = set()
    complex_patterns: list[str] = []
    for g in glob_patterns:
        if g.startswith("**/") and "*" not in g[3:]:
            suffix = g[3:]  # e.g., "*.go" → extract ".go"
            if suffix.startswith("*."):
                suffixes.add(suffix[1:])  # ".go"
            else:
                complex_patterns.append(g)
        else:
            complex_patterns.append(g)

    result: list[str] = []
    for f in tracked_files:
        if len(result) >= max_files:
            break
        # Fast path: suffix set lookup
        if suffixes and any(f.endswith(s) for s in suffixes):
            result.append(f)
        elif complex_patterns and any(matches_glob(f, g) for g in complex_patterns):
            result.append(f)
    return result


def _compile_patterns(family: TaskFamily) -> list[tuple[str, re.Pattern[str]]]:
    """Compile content regex patterns, skipping invalid ones."""
    compiled = []
    for p in family.content_patterns:
        try:
            compiled.append((p, re.compile(p)))
        except re.error:
            logger.warning("Invalid regex in family %s: %s", family.name, p)
    return compiled


def _scan_files(
    repo_path: Path,
    candidate_files: list[str],
    compiled_patterns: list[tuple[str, re.Pattern[str]]],
    max_hits: int,
) -> tuple[list[PatternHit], set[str]]:
    """Scan files for pattern matches. Returns (hits, matched_files)."""
    hits: list[PatternHit] = []
    matched_files: set[str] = set()

    for file_path in candidate_files:
        full_path = repo_path / file_path
        try:
            if full_path.stat().st_size > _MAX_FILE_SIZE:
                continue
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
                    break

        if len(hits) >= max_hits:
            break

    return hits[:max_hits], matched_files


def scan_repo(
    repo_path: Path,
    families: tuple[TaskFamily, ...] | None = None,
    *,
    max_files: int = 50_000,
    tracked_files: frozenset[str] | None = None,
) -> list[FamilyScanResult]:
    """Scan a repo for all task families. Returns results with enough hits."""
    if families is None:
        families = FAMILIES

    if tracked_files is None:
        tracked_files = get_tracked_files(repo_path)
    commit_sha = get_head_sha(repo_path)
    results: list[FamilyScanResult] = []

    for family in families:
        result = scan_repo_for_family(
            repo_path,
            family,
            max_files=max_files,
            tracked_files=tracked_files,
            commit_sha=commit_sha,
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


# ---------------------------------------------------------------------------
# Multi-hop: find callers of deprecated symbols
# ---------------------------------------------------------------------------

_DEPRECATED_MARKERS = re.compile(
    r"@[Dd]eprecated|#\[deprecated|//\s*Deprecated:", re.MULTILINE
)

_COMMON_SYMBOLS = frozenset(
    {
        "Config",
        "List",
        "Get",
        "Set",
        "New",
        "Run",
        "Start",
        "Stop",
        "Close",
        "Open",
        "Read",
        "Write",
        "Delete",
        "Update",
        "Create",
        "Init",
        "Test",
        "Error",
        "String",
        "Type",
        "Name",
        "Value",
        "Handle",
        "Status",
        "Result",
        "Context",
        "Options",
        "Spec",
        "Data",
        "Info",
        "Key",
        "Event",
        "Node",
        "Item",
        "Map",
        "Func",
        "Watch",
        "Add",
        "Remove",
        "Check",
        "Validate",
        "Parse",
        "Format",
    }
)

_MIN_SYMBOL_LEN = 6
_MAX_MULTI_HOP_FILES = 200
_SOURCE_EXTS = (".go", ".py", ".java", ".ts", ".js", ".rs")


def _extract_deprecated_symbols(
    repo_path: Path,
    symbol_files: frozenset[str],
    language: str,
) -> set[str]:
    """Extract symbol names near deprecated annotations in the given files."""
    symbols: set[str] = set()
    for file_path in symbol_files:
        full_path = repo_path / file_path
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        lines = content.splitlines()
        for i, line in enumerate(lines):
            window = "\n".join(lines[max(0, i - 2) : i + 1])
            if not _DEPRECATED_MARKERS.search(window):
                continue

            m = None
            if language == "go":
                m = re.search(r"^func\s+(?:\([^)]+\)\s+)?(\w+)", line)
                if not m:
                    m = re.search(r"^type\s+(\w+)", line)
            elif language == "python":
                m = re.search(r"^(?:def|class)\s+(\w+)", line)
            elif language == "java":
                m = re.search(
                    r"(?:public|private|protected)?\s*"
                    r"(?:static\s+)?(?:\w+\s+)+(\w+)\s*\(",
                    line,
                )
            if m:
                symbols.add(m.group(1))

    return {
        s for s in symbols if s not in _COMMON_SYMBOLS and len(s) >= _MIN_SYMBOL_LEN
    }


def find_callers_of_symbols(
    repo_path: Path,
    symbol_files: frozenset[str],
    tracked_files: frozenset[str],
    language: str,
    *,
    max_files: int = 50_000,
) -> frozenset[str]:
    """Find files referencing deprecated symbols from the given files."""
    symbols = _extract_deprecated_symbols(repo_path, symbol_files, language)
    if not symbols:
        return frozenset()

    logger.info("Multi-hop: %d symbols: %s", len(symbols), sorted(symbols)[:10])

    pattern = re.compile(r"\b(" + "|".join(re.escape(s) for s in symbols) + r")\b")
    caller_files: set[str] = set()
    scanned = 0

    for file_path in tracked_files:
        if file_path in symbol_files or scanned >= max_files:
            break
        if len(caller_files) >= _MAX_MULTI_HOP_FILES:
            break
        if not any(file_path.endswith(ext) for ext in _SOURCE_EXTS):
            continue
        full_path = repo_path / file_path
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1
        if pattern.search(content):
            caller_files.add(file_path)

    return frozenset(caller_files)


# ---------------------------------------------------------------------------
# Dep-trace: discover specific imported packages
# ---------------------------------------------------------------------------

_GO_IMPORT_PATTERN = re.compile(r'"([^"]+)"')
_PY_IMPORT_PATTERN = re.compile(r"^(?:from|import)\s+([\w.]+)")
_MAX_DEP_TRACE_PACKAGES = 3
_MAX_DEP_GT = 500

_PYTHON_STDLIB_SKIP = frozenset(
    {
        "os",
        "sys",
        "re",
        "json",
        "typing",
        "io",
        "warnings",
        "collections",
        "datetime",
        "functools",
        "itertools",
        "operator",
        "pathlib",
        "copy",
        "contextlib",
        "inspect",
        "textwrap",
        "string",
        "math",
        "abc",
        "dataclasses",
        "enum",
        "hashlib",
        "hmac",
        "secrets",
        "struct",
        "subprocess",
        "threading",
        "multiprocessing",
        "logging",
        "argparse",
        "configparser",
        "csv",
        "pickle",
        "shelve",
        "sqlite3",
        "xml",
        "html",
        "http",
        "urllib",
        "email",
        "socket",
        "ssl",
        "select",
        "signal",
        "shutil",
        "tempfile",
        "glob",
        "fnmatch",
        "stat",
        "time",
        "calendar",
        "locale",
        "gettext",
        "unicodedata",
        "codecs",
        "pprint",
        "traceback",
        "types",
        "weakref",
        "array",
        "bisect",
        "heapq",
        "queue",
        "decimal",
        "fractions",
        "random",
        "statistics",
        "ctypes",
        "platform",
        "importlib",
        "pkgutil",
        "zipfile",
        "tarfile",
        "gzip",
        "bz2",
        "lzma",
        "zlib",
        "base64",
        "binascii",
        "difflib",
        "unittest",
        "doctest",
        "pdb",
        "cProfile",
        "profile",
        "timeit",
        "dis",
        "ast",
        "token",
        "tokenize",
        "keyword",
        "compileall",
        "py_compile",
        "code",
        "codeop",
        "atexit",
        "gc",
        "site",
        "builtins",
        "__future__",
        "zoneinfo",
        "uuid",
        "concurrent",
        "asyncio",
        "contextvars",
        "numbers",
    }
)

_TEST_PACKAGES = frozenset({"pytest", "unittest", "nose", "hypothesis", "mock"})


def discover_top_imports(
    repo_path: Path,
    tracked_files: frozenset[str],
    language: str,
    *,
    max_files: int = 5000,
) -> list[tuple[str, frozenset[str]]]:
    """Discover external packages imported by 10+ files.

    Returns up to _MAX_DEP_TRACE_PACKAGES as (package_name, importing_files).
    Filters out stdlib, self-imports, and test frameworks.
    """
    import_counts: Counter[str] = Counter()
    import_files: dict[str, set[str]] = {}

    ext_map = {
        "go": (".go",),
        "python": (".py",),
        "java": (".java",),
        "typescript": (".ts", ".tsx"),
        "javascript": (".js", ".jsx"),
    }
    valid_exts = ext_map.get(language, (".go",))

    scanned = 0
    for file_path in tracked_files:
        if scanned >= max_files:
            break
        if not any(file_path.endswith(ext) for ext in valid_exts):
            continue
        if "vendor/" in file_path or "testdata/" in file_path:
            continue
        full_path = repo_path / file_path
        try:
            content = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        scanned += 1

        if language == "go":
            _collect_go_imports(content, file_path, import_counts, import_files)
        elif language == "python":
            _collect_python_imports(content, file_path, import_counts, import_files)

    # Filter self-imports and test frameworks
    repo_name = repo_path.name.lower().replace("-", "_")
    for skip in (repo_name, repo_path.name, *_TEST_PACKAGES):
        import_counts.pop(skip, None)
        import_files.pop(skip, None)

    # Return packages with 10+ importers, capped at _MAX_DEP_GT
    candidates = []
    for pkg, _cnt in import_counts.most_common(50):
        files = import_files.get(pkg, set())
        if len(files) >= 10:
            candidates.append((pkg, len(files), frozenset(sorted(files)[:_MAX_DEP_GT])))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return [(pkg, files) for pkg, _n, files in candidates[:_MAX_DEP_TRACE_PACKAGES]]


def _collect_go_imports(
    content: str,
    file_path: str,
    counts: Counter[str],
    files: dict[str, set[str]],
) -> None:
    for m in _GO_IMPORT_PATTERN.finditer(content):
        pkg = m.group(1)
        if "/" in pkg and "." in pkg.split("/")[0]:
            parts = pkg.split("/")
            key = "/".join(parts[: min(3, len(parts))])
            counts[key] += 1
            files.setdefault(key, set()).add(file_path)


def _collect_python_imports(
    content: str,
    file_path: str,
    counts: Counter[str],
    files: dict[str, set[str]],
) -> None:
    for line in content.splitlines():
        m = _PY_IMPORT_PATTERN.match(line)
        if m:
            pkg = m.group(1).split(".")[0]
            if pkg and pkg not in _PYTHON_STDLIB_SKIP:
                counts[pkg] += 1
                files.setdefault(pkg, set()).add(file_path)
