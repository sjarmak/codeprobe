"""Structural scanner for org-scale task mining.

Scans codebases for structural patterns (deprecated APIs, security configs,
import dependencies) using globs + regex. ZFC compliant: mechanism only.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections import Counter
from dataclasses import dataclass, field
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
    """Results of scanning one or more repos for one task family."""

    family: TaskFamily
    hits: tuple[PatternHit, ...]
    repo_paths: tuple[Path, ...]
    commit_sha: str
    matched_files: frozenset[str]
    timed_out: bool = field(default=False)


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

# ---------------------------------------------------------------------------
# Scan result cache
# ---------------------------------------------------------------------------

_scan_cache: dict[tuple[tuple[str, ...], str, str], FamilyScanResult] = {}


def clear_scan_cache() -> None:
    """Clear the module-level scan result cache."""
    _scan_cache.clear()


def _cache_key(
    repo_paths: list[Path], commit_sha: str, family_name: str
) -> tuple[tuple[str, ...], str, str]:
    return (tuple(sorted(str(p) for p in repo_paths)), commit_sha, family_name)


def scan_repo_for_family(
    repo_paths: list[Path],
    family: TaskFamily,
    *,
    max_files: int = 50_000,
    tracked_files: frozenset[str] | None = None,
    commit_sha: str = "",
    timeout_seconds: float = 60.0,
) -> FamilyScanResult:
    """Scan one or more repos for pattern matches belonging to a task family."""
    # Gather per-repo tracked files and commit SHAs
    per_repo: list[tuple[Path, frozenset[str], str]] = []
    for rp in repo_paths:
        tf = tracked_files if tracked_files is not None else get_tracked_files(rp)
        sha = commit_sha if commit_sha else get_head_sha(rp)
        per_repo.append((rp, tf, sha))

    combined_sha = ",".join(sha for _, _, sha in per_repo)

    # Check cache
    key = _cache_key(repo_paths, combined_sha, family.name)
    if key in _scan_cache:
        return _scan_cache[key]

    compiled_patterns = _compile_patterns(family)
    deadline = time.monotonic() + timeout_seconds

    all_hits: list[PatternHit] = []
    all_matched_files: set[str] = set()
    any_timed_out = False

    for rp, tf, _sha in per_repo:
        candidate_files = _filter_by_suffix(tf, family.glob_patterns, max_files)
        hits, matched_files, timed_out = _scan_files(
            rp,
            candidate_files,
            compiled_patterns,
            family.max_hits,
            deadline=deadline,
        )
        all_hits.extend(hits)
        all_matched_files.update(matched_files)
        if timed_out:
            any_timed_out = True
            break

    result = FamilyScanResult(
        family=family,
        hits=tuple(all_hits),
        repo_paths=tuple(repo_paths),
        commit_sha=combined_sha,
        matched_files=frozenset(all_matched_files),
        timed_out=any_timed_out,
    )
    _scan_cache[key] = result
    return result


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
        # Skip vendored / generated / test-data directories
        if any(seg in f for seg in ("vendor/", "node_modules/", "testdata/")):
            continue
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
    *,
    deadline: float | None = None,
) -> tuple[list[PatternHit], set[str], bool]:
    """Scan files for pattern matches. Returns (hits, matched_files, timed_out)."""
    hits: list[PatternHit] = []
    matched_files: set[str] = set()
    timed_out = False

    for file_path in candidate_files:
        if deadline is not None and time.monotonic() > deadline:
            timed_out = True
            break

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

    return hits[:max_hits], matched_files, timed_out


def scan_repo(
    repo_paths: list[Path],
    families: tuple[TaskFamily, ...] | None = None,
    *,
    max_files: int = 50_000,
    tracked_files: frozenset[str] | None = None,
    timeout_seconds: float = 60.0,
) -> list[FamilyScanResult]:
    """Scan one or more repos for all task families. Returns results with enough hits."""
    if families is None:
        families = FAMILIES

    results: list[FamilyScanResult] = []

    for family in families:
        result = scan_repo_for_family(
            repo_paths,
            family,
            max_files=max_files,
            tracked_files=tracked_files,
            timeout_seconds=timeout_seconds,
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
    repo_paths: list[Path],
    symbol_files: frozenset[str],
    tracked_files: frozenset[str],
    language: str,
    *,
    max_files: int = 50_000,
) -> frozenset[str]:
    """Find files referencing deprecated symbols across one or more repos."""
    # Extract symbols from all repos
    all_symbols: set[str] = set()
    for rp in repo_paths:
        all_symbols.update(_extract_deprecated_symbols(rp, symbol_files, language))
    if not all_symbols:
        return frozenset()

    logger.info("Multi-hop: %d symbols: %s", len(all_symbols), sorted(all_symbols)[:10])

    pattern = re.compile(r"\b(" + "|".join(re.escape(s) for s in all_symbols) + r")\b")
    caller_files: set[str] = set()

    for rp in repo_paths:
        scanned = 0
        rp_tracked = tracked_files if tracked_files else get_tracked_files(rp)
        for file_path in rp_tracked:
            if file_path in symbol_files or scanned >= max_files:
                break
            if len(caller_files) >= _MAX_MULTI_HOP_FILES:
                break
            if not any(file_path.endswith(ext) for ext in _SOURCE_EXTS):
                continue
            full_path = rp / file_path
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
    repo_paths: list[Path],
    tracked_files: frozenset[str],
    language: str,
    *,
    max_files: int = 5000,
) -> list[tuple[str, frozenset[str]]]:
    """Discover external packages imported by 10+ files across one or more repos.

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

    for rp in repo_paths:
        rp_tracked = tracked_files if tracked_files else get_tracked_files(rp)
        scanned = 0
        for file_path in rp_tracked:
            if scanned >= max_files:
                break
            if not any(file_path.endswith(ext) for ext in valid_exts):
                continue
            if "vendor/" in file_path or "testdata/" in file_path:
                continue
            full_path = rp / file_path
            try:
                content = full_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            scanned += 1

            if language == "go":
                _collect_go_imports(content, file_path, import_counts, import_files)
            elif language == "python":
                _collect_python_imports(content, file_path, import_counts, import_files)

    # Filter self-imports and test frameworks for each repo
    for rp in repo_paths:
        repo_name = rp.name.lower().replace("-", "_")
        for skip in (repo_name, rp.name, *_TEST_PACKAGES):
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


# ---------------------------------------------------------------------------
# MCP-advantaged: discover high-fan-out public symbols for reference tracing
# ---------------------------------------------------------------------------

# Common names that appear everywhere and produce useless tasks
_SYMBOL_BLOCKLIST = frozenset(
    {
        "array",
        "test",
        "setup",
        "error",
        "result",
        "value",
        "values",
        "config",
        "create",
        "delete",
        "update",
        "insert",
        "select",
        "main",
        "init",
        "string",
        "buffer",
        "assert",
        "return",
        "params",
        "output",
        "input",
        "number",
        "format",
        "object",
        "source",
        "target",
        "handle",
        "render",
        "length",
        "record",
        "status",
        "module",
        "method",
        "column",
        "reshape",
        "append",
        "extend",
        "remove",
        "astype",
        "asarray",
        "flatten",
    }
)

_DEF_PATTERN_PY = re.compile(r"^\s*def\s+(\w{8,})\s*\(", re.MULTILINE)
_DEF_PATTERN_GO = re.compile(
    r"^func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w{8,})\(", re.MULTILINE
)
_CLASS_PATTERN = re.compile(r"^\s*class\s+(\w{8,})\s*[:\(]", re.MULTILINE)

_MAX_REFERENCE_TARGETS = 3
_MAX_REF_GT = 300


def _is_language_test_file(file_path: str, language: str) -> bool:
    """Detect language-convention test files for definition-extraction skip.

    Path-substring filters like ``("/test", "/vendor/")`` only catch directory
    layouts (``/tests/foo.py``); they miss Go's ``foo_test.go`` and Python's
    ``test_foo.py``/``foo_test.py`` conventions where test files live next to
    source. A mock implementation in a test file (e.g.
    ``func (m mockFS) MkdirAll(...)``) must not be recorded as the definition
    site for the production symbol.
    """
    name = file_path.rsplit("/", 1)[-1]
    if language == "go":
        return name.endswith("_test.go")
    if language == "python":
        return name.startswith("test_") or name.endswith("_test.py")
    # Mixed/unknown: be conservative and exclude both conventions.
    return (
        name.endswith("_test.go")
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _extract_symbol_definitions(
    repo_paths: list[Path],
    tracked_files: frozenset[str],
    language: str,
    *,
    extra_exclude_path_fragments: tuple[str, ...] = (),
    min_symbol_length: int = 0,
    exported_only: bool = False,
) -> dict[str, str]:
    """Phase 1 shared helper: extract symbol-definition map from tracked files.

    Returns ``{symbol_name: defining_file_repo_relative}`` for symbols that
    pass the filters (exported-ish names, not in vendor/test paths).

    Callers can tighten the filter via ``extra_exclude_path_fragments``,
    ``min_symbol_length``, and ``exported_only`` (Go/Python convention: first
    letter uppercase) — used by the SG-driven discovery path to reduce the
    candidate set before making MCP calls.
    """
    if language == "python":
        patterns = [_DEF_PATTERN_PY, _CLASS_PATTERN]
        exts = {".py"}
    elif language == "go":
        patterns = [_DEF_PATTERN_GO]
        exts = {".go"}
    else:
        patterns = [_DEF_PATTERN_PY, _CLASS_PATTERN]
        exts = set(_SOURCE_EXTS)

    exclude_fragments = ("/test", "/vendor/") + extra_exclude_path_fragments

    symbol_defs: dict[str, str] = {}
    for rp in repo_paths:
        for file_path in tracked_files:
            if not any(file_path.endswith(e) for e in exts):
                continue
            # Skip language-convention test files (Go _test.go,
            # Python test_*.py / *_test.py). A mock impl in a test file
            # must not shadow the real definition.
            if _is_language_test_file(file_path, language):
                continue
            # Prepend a slash so top-level paths (e.g. ``staging/src/...``)
            # match fragments like ``/staging/`` without needing a separate
            # startswith check.
            normalized = "/" + file_path
            if any(frag in normalized for frag in exclude_fragments):
                continue
            full = rp / file_path
            try:
                content = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pat in patterns:
                for m in pat.finditer(content):
                    name = m.group(1)
                    if len(name) < min_symbol_length:
                        continue
                    if name.startswith("_") or name.lower() in _SYMBOL_BLOCKLIST:
                        continue
                    if exported_only and not name[:1].isupper():
                        continue
                    # Require domain-specific names: has underscore (snake_case)
                    # or mixed case (CamelCase). Rejects single-word names like
                    # "contains", "multiply", "startswith".
                    has_underscore = "_" in name
                    has_mixed_case = name != name.lower() and name != name.upper()
                    if not (has_underscore or has_mixed_case):
                        continue
                    if name not in symbol_defs:
                        symbol_defs[name] = file_path

    return symbol_defs


def discover_reference_targets(
    repo_paths: list[Path],
    tracked_files: frozenset[str],
    language: str,
) -> list[tuple[str, str, frozenset[str]]]:
    """Find high-fan-out public symbols suitable for reference-trace tasks.

    Returns ``[(symbol_name, defining_file, referencing_files), ...]`` sorted
    by reference count descending, capped at ``_MAX_REFERENCE_TARGETS``.
    """
    # Phase 1: collect symbol definitions from non-test source files
    symbol_defs = _extract_symbol_definitions(
        repo_paths, tracked_files, language
    )

    if not symbol_defs:
        return []

    if language == "python":
        exts = {".py"}
    elif language == "go":
        exts = {".go"}
    else:
        exts = set(_SOURCE_EXTS)

    logger.info(
        "Reference targets: %d candidate symbols from %d files",
        len(symbol_defs),
        len({v for v in symbol_defs.values()}),
    )

    # Phase 2: count references for each symbol across all source files
    # Use batched regex to avoid scanning the codebase N times
    # Process in chunks to keep regex size manageable
    chunk_size = 200
    symbol_list = list(symbol_defs.keys())
    ref_counts: Counter[str] = Counter()
    ref_files: dict[str, set[str]] = {s: set() for s in symbol_list}

    for chunk_start in range(0, len(symbol_list), chunk_size):
        chunk = symbol_list[chunk_start : chunk_start + chunk_size]
        pattern = re.compile(r"\b(" + "|".join(re.escape(s) for s in chunk) + r")\b")

        for rp in repo_paths:
            for file_path in tracked_files:
                if not any(file_path.endswith(e) for e in exts):
                    continue
                full = rp / file_path
                try:
                    content = full.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                found_in_file: set[str] = set()
                for m in pattern.finditer(content):
                    found_in_file.add(m.group(1))
                for sym in found_in_file:
                    # Don't count the defining file as a reference
                    if file_path != symbol_defs.get(sym):
                        ref_counts[sym] += 1
                        ref_files[sym].add(file_path)

    # Phase 3: select top symbols with 10+ references
    candidates = []
    for sym, count in ref_counts.most_common(50):
        if count < 10:
            break
        files = frozenset(sorted(ref_files[sym])[:_MAX_REF_GT])
        candidates.append((sym, symbol_defs[sym], files))

    return candidates[:_MAX_REFERENCE_TARGETS]


# ---------------------------------------------------------------------------
# Sourcegraph-driven reference-target discovery
# ---------------------------------------------------------------------------

# Path fragments excluded from SG-mode candidate selection. These are
# structural filters (generated / re-exported / test surfaces), not semantic
# judgments — they narrow the search space so we don't waste MCP calls on
# noise like zz_generated.openapi.go or staging/ shadows.
_SG_MODE_EXTRA_EXCLUDES: tuple[str, ...] = (
    "/staging/",
    "/zz_generated",
    "/applyconfigurations/",
    "_test.go",
    "/fuzzer/",
    "/testdata/",
    "/internal/",  # Go idiom for package-private APIs with no cross-pkg refs
)

# Default sample size: how many candidates to rank via Sourcegraph. Keeping
# this bounded keeps MCP wall-clock predictable (sample_size × ~1s per call).
_SG_MODE_SAMPLE_SIZE = 100

# Minimum SG reference count for a symbol to be worth considering at all.
# The local-grep path uses 10 because it counts same-file references; SG
# counts distinct files and is stricter, so this is lower.
_SG_MODE_MIN_REFS = 2


def discover_reference_targets_via_sg(
    repo_paths: list[Path],
    tracked_files: frozenset[str],
    language: str,
    *,
    repo_sg_name: str,
    sg_url: str = "https://demo.sourcegraph.com",
    sample_size: int = _SG_MODE_SAMPLE_SIZE,
    max_targets: int = _MAX_REFERENCE_TARGETS,
    max_workers: int = 8,
    random_seed: int = 42,
) -> list[tuple[str, str, frozenset[str]]]:
    """Sourcegraph-driven reference-target discovery.

    Replaces the local grep-based Phase 2 (which scans every source file
    for every candidate symbol — O(N_files × N_symbols) and can run for
    hours on a repo like kubernetes) with a bounded number of Sourcegraph
    ``sg_find_references`` MCP calls.

    Trade-off: we sample candidates rather than exhaustively scoring every
    symbol Phase 1 finds. The sample is deterministic (seeded) so reruns
    are reproducible. Sample size × SG wall-clock sets the mining budget.

    Returns the same shape as :func:`discover_reference_targets`:
    ``[(symbol_name, defining_file, referencing_files), ...]`` sorted by
    reference count descending, capped at ``max_targets``.
    """
    import random
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from codeprobe.mining.sg_ground_truth import _call_find_references

    symbol_defs = _extract_symbol_definitions(
        repo_paths,
        tracked_files,
        language,
        extra_exclude_path_fragments=_SG_MODE_EXTRA_EXCLUDES,
        min_symbol_length=6,
        exported_only=True,
    )

    if not symbol_defs:
        return []

    candidates = sorted(symbol_defs.items())  # stable order for seeded sample
    effective_sample = min(sample_size, len(candidates))
    rng = random.Random(random_seed)
    sample = rng.sample(candidates, effective_sample)

    logger.info(
        "SG discovery: %d candidate symbols after filtering, sampling %d",
        len(candidates),
        effective_sample,
    )

    def _score(
        sym: str, def_file: str
    ) -> tuple[int, str, str, frozenset[str]]:
        refs = _call_find_references(
            symbol=sym,
            defining_file=def_file,
            repo_sg_name=repo_sg_name,
            sg_url=sg_url,
        )
        if refs is None:
            return (-1, sym, def_file, frozenset())
        return (len(refs), sym, def_file, refs)

    scored: list[tuple[int, str, str, frozenset[str]]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_score, sym, def_file) for sym, def_file in sample]
        for f in as_completed(futures):
            result = f.result()
            if result[0] >= _SG_MODE_MIN_REFS:
                scored.append(result)

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:max_targets]

    logger.info(
        "SG discovery: %d symbols ranked, returning top %d",
        len(scored),
        len(top),
    )

    return [
        (sym, def_file, frozenset(sorted(refs)[:_MAX_REF_GT]))
        for _, sym, def_file, refs in top
    ]


# ---------------------------------------------------------------------------
# MCP-advantaged: discover base types with multiple implementations
# ---------------------------------------------------------------------------

_BASE_CLASS_PATTERNS = (
    re.compile(r"class\s+(\w{4,})\(.*(?:ABC|Protocol)\w*", re.MULTILINE),
    re.compile(r"class\s+(\w{4,})\(.*metaclass=ABCMeta", re.MULTILINE),
    re.compile(r"^\s*class\s+(\w{4,}).*:\s*$", re.MULTILINE),  # broad catch
)

_SUBCLASS_TEMPLATE = r"class\s+\w+\(.*\b{base}\b"

_MAX_BASE_TYPES = 3


def discover_base_types(
    repo_paths: list[Path],
    tracked_files: frozenset[str],
    language: str,
) -> list[tuple[str, str, frozenset[str], frozenset[str]]]:
    """Find abstract base classes / Protocols with 3+ concrete subclasses.

    Returns ``[(base_name, defining_file, subclass_files, usage_files), ...]``.
    ``usage_files`` are files that reference concrete subclasses by name
    (grep baseline for the type hierarchy).
    """
    if language != "python":
        return []  # TODO: extend to Go interfaces, Java/TS

    exts = {".py"}

    # Phase 1: find ABC / Protocol definitions
    base_defs: dict[str, str] = {}  # base_name -> defining_file
    for rp in repo_paths:
        for file_path in tracked_files:
            if not any(file_path.endswith(e) for e in exts):
                continue
            if "/test" in file_path or "/vendor/" in file_path:
                continue
            full = rp / file_path
            try:
                content = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # Look for ABC/Protocol base classes only (not broad catch)
            for pat in _BASE_CLASS_PATTERNS[:2]:
                for m in pat.finditer(content):
                    name = m.group(1)
                    if name.startswith("_") or name.lower() in _SYMBOL_BLOCKLIST:
                        continue
                    if name not in base_defs:
                        base_defs[name] = file_path

    if not base_defs:
        return []

    logger.info("Base types: %d candidates", len(base_defs))

    # Phase 2: find subclasses for each base type
    results: list[tuple[str, str, frozenset[str], frozenset[str]]] = []
    for base_name, def_file in base_defs.items():
        subclass_pat = re.compile(_SUBCLASS_TEMPLATE.format(base=re.escape(base_name)))
        subclass_files: set[str] = set()
        subclass_names: set[str] = set()

        for rp in repo_paths:
            for file_path in tracked_files:
                if not any(file_path.endswith(e) for e in exts):
                    continue
                full = rp / file_path
                try:
                    content = full.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                for m in subclass_pat.finditer(content):
                    subclass_files.add(file_path)
                    # Extract the subclass name
                    sub_match = re.match(r"class\s+(\w+)", m.group(0))
                    if sub_match:
                        subclass_names.add(sub_match.group(1))

        if len(subclass_files) < 3:
            continue

        # Phase 3: find usage sites for the concrete subclass names
        if subclass_names:
            usage_pat = re.compile(
                r"\b(" + "|".join(re.escape(s) for s in subclass_names) + r")\b"
            )
            usage_files: set[str] = set()
            for rp in repo_paths:
                for file_path in tracked_files:
                    if not any(file_path.endswith(e) for e in exts):
                        continue
                    if file_path in subclass_files or file_path == def_file:
                        continue
                    full = rp / file_path
                    try:
                        content = full.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    if usage_pat.search(content):
                        usage_files.add(file_path)
        else:
            usage_files = set()

        all_files = subclass_files | usage_files
        if len(all_files) >= 5:
            results.append(
                (
                    base_name,
                    def_file,
                    frozenset(subclass_files),
                    frozenset(usage_files),
                )
            )

    results.sort(key=lambda x: len(x[2]) + len(x[3]), reverse=True)
    return results[:_MAX_BASE_TYPES]


# ---------------------------------------------------------------------------
# MCP-advantaged: discover recently changed public symbols for scope audit
# ---------------------------------------------------------------------------

_DEF_IN_DIFF_PY = re.compile(r"^\+\s*def\s+(\w+)\s*\(", re.MULTILINE)
_DEF_IN_DIFF_GO = re.compile(r"^\+func\s+(?:\(\w+\s+\*?\w+\)\s+)?(\w+)\(", re.MULTILINE)
_CLASS_IN_DIFF = re.compile(r"^\+\s*class\s+(\w+)\s*[:\(]", re.MULTILINE)
_COMMIT_BOUNDARY = re.compile(r"^([0-9a-f]{40})$", re.MULTILINE)

_MAX_CHANGED_SYMBOLS = 3
_MIN_CHANGED_REFS = 5
_MIN_CHANGED_SYMBOL_LEN = 8


def discover_changed_symbols(
    repo_paths: list[Path],
    tracked_files: frozenset[str],
    language: str,
    *,
    recent_n: int = 50,
) -> list[tuple[str, str, str, frozenset[str]]]:
    """Find recently changed public symbols and their dependents.

    Parses recent git commits for modified function/class definitions,
    then greps all tracked files for references to those symbols.

    Returns ``[(commit_sha, symbol_name, defining_file, referencing_files), ...]``
    sorted by reference count descending, capped at ``_MAX_CHANGED_SYMBOLS``.
    """
    if recent_n <= 0:
        return []

    if language == "python":
        diff_patterns = [_DEF_IN_DIFF_PY, _CLASS_IN_DIFF]
        exts = {".py"}
        ext_glob = "*.py"
    elif language == "go":
        diff_patterns = [_DEF_IN_DIFF_GO]
        exts = {".go"}
        ext_glob = "*.go"
    else:
        diff_patterns = [_DEF_IN_DIFF_PY, _CLASS_IN_DIFF]
        exts = set(_SOURCE_EXTS)
        ext_glob = "*.py"

    # Phase 1: extract modified symbols from recent diffs
    # commit_sha -> {(symbol_name, defining_file)}
    commit_symbols: dict[str, list[tuple[str, str]]] = {}

    for rp in repo_paths:
        try:
            result = subprocess.run(
                [
                    "git",
                    "log",
                    "--diff-filter=M",
                    "-p",
                    "--format=%H",
                    f"-n{recent_n}",
                    "--",
                    ext_glob,
                ],
                cwd=str(rp),
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
            if result.returncode != 0:
                continue
        except (subprocess.TimeoutExpired, OSError):
            continue

        _parse_diff_output(result.stdout, diff_patterns, commit_symbols)

    if not commit_symbols:
        return []

    # Phase 2: count references for each symbol across tracked files
    # Flatten all unique symbols first
    all_symbols: dict[str, tuple[str, str]] = {}  # symbol -> (commit, file)
    for sha, pairs in commit_symbols.items():
        for sym, def_file in pairs:
            if sym not in all_symbols:
                all_symbols[sym] = (sha, def_file)

    if not all_symbols:
        return []

    # Batched regex reference counting (same pattern as discover_reference_targets)
    chunk_size = 200
    symbol_list = list(all_symbols.keys())
    ref_counts: Counter[str] = Counter()
    ref_files: dict[str, set[str]] = {s: set() for s in symbol_list}

    for chunk_start in range(0, len(symbol_list), chunk_size):
        chunk = symbol_list[chunk_start : chunk_start + chunk_size]
        pattern = re.compile(r"\b(" + "|".join(re.escape(s) for s in chunk) + r")\b")

        for rp in repo_paths:
            for file_path in tracked_files:
                if not any(file_path.endswith(e) for e in exts):
                    continue
                full = rp / file_path
                try:
                    content = full.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                found_in_file: set[str] = set()
                for m in pattern.finditer(content):
                    found_in_file.add(m.group(1))
                for sym in found_in_file:
                    _sha, def_file = all_symbols[sym]
                    if file_path != def_file:
                        ref_counts[sym] += 1
                        ref_files[sym].add(file_path)

    # Phase 3: select top symbols with _MIN_CHANGED_REFS+ references
    candidates: list[tuple[str, str, str, frozenset[str]]] = []
    for sym, count in ref_counts.most_common(50):
        if count < _MIN_CHANGED_REFS:
            break
        sha, def_file = all_symbols[sym]
        files = frozenset(sorted(ref_files[sym])[:_MAX_REF_GT])
        candidates.append((sha, sym, def_file, files))

    return candidates[:_MAX_CHANGED_SYMBOLS]


def _parse_diff_output(
    diff_text: str,
    diff_patterns: list[re.Pattern[str]],
    commit_symbols: dict[str, list[tuple[str, str]]],
) -> None:
    """Parse git log -p output to extract modified symbols per commit.

    Populates ``commit_symbols`` mapping commit SHA to list of
    ``(symbol_name, defining_file)`` tuples.
    """
    current_sha: str = ""
    current_file: str = ""

    for line in diff_text.splitlines():
        # Detect commit boundary
        sha_match = _COMMIT_BOUNDARY.match(line)
        if sha_match:
            current_sha = sha_match.group(1)
            continue

        # Detect file being diffed
        if line.startswith("diff --git"):
            # "diff --git a/path/file.py b/path/file.py"
            parts = line.split(" b/", 1)
            if len(parts) == 2:
                current_file = parts[1]
            continue

        # Only look at added/modified lines (starting with +)
        if not line.startswith("+") or line.startswith("+++"):
            continue

        if not current_sha:
            continue

        for pat in diff_patterns:
            m = pat.match(line)
            if m:
                name = m.group(1)
                if (
                    len(name) >= _MIN_CHANGED_SYMBOL_LEN
                    and not name.startswith("_")
                    and name.lower() not in _SYMBOL_BLOCKLIST
                ):
                    # Require domain-specificity: snake_case or CamelCase
                    has_underscore = "_" in name
                    has_mixed_case = name != name.lower() and name != name.upper()
                    if has_underscore or has_mixed_case:
                        commit_symbols.setdefault(current_sha, []).append(
                            (name, current_file)
                        )
                break


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
