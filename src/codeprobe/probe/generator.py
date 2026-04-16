"""Extract symbols from Python/TypeScript repos and generate micro-benchmark probes.

Ported from ~/projects/MCP-Eval-Tasks/scripts/generate_probes.py with adaptations
for the codeprobe package structure.
"""

from __future__ import annotations

import logging
import os
import random
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_COUNT = 30
MIN_PROBES = 5
MAX_PROBES = 50
SLOW_GENERATION_THRESHOLD_SEC = 60

SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "site-packages",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        "dist",
        "build",
        ".next",
        ".nuxt",
        "target",
        "vendor",
        ".gradle",
        ".idea",
        ".vscode",
        "coverage",
        ".eggs",
        "egg-info",
    }
)

SKIP_FILE_PATTERNS = re.compile(
    r"(\.min\.|\.bundle\.|\.generated\.|\.d\.ts$|__init__\.py$|setup\.py$)"
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Symbol:
    """A code symbol extracted from the repository."""

    name: str
    kind: str  # "function", "class", "method"
    file_path: str  # relative to repo root
    line: int
    class_name: str | None = None  # for methods
    return_type: str | None = None


@dataclass(frozen=True)
class Probe:
    """A generated probe task."""

    template_name: str
    category: str
    prompt: str
    answer: str
    answer_type: str
    difficulty: str
    capability_tags: tuple[str, ...] = field(default_factory=tuple)
    time_limit_sec: int = 30


# ---------------------------------------------------------------------------
# Symbol extraction — Python
# ---------------------------------------------------------------------------

PY_FUNCTION_RE = re.compile(
    r"^(?P<indent>\s*)def\s+(?P<name>[a-zA-Z_]\w*)\s*\((?P<params>[^)]*)\)"
    r"(?:\s*->\s*(?P<return>[^:]+))?:",
    re.MULTILINE,
)
PY_CLASS_RE = re.compile(
    r"^class\s+(?P<name>[a-zA-Z_]\w*)\s*(?:\([^)]*\))?\s*:",
    re.MULTILINE,
)


def extract_python_symbols(content: str, rel_path: str) -> list[Symbol]:
    """Extract function, class, and method symbols from Python source."""
    symbols: list[Symbol] = []
    current_class: str | None = None
    class_indent: int = -1

    for line_num, line in enumerate(content.splitlines(), start=1):
        class_match = PY_CLASS_RE.match(line)
        if class_match:
            current_class = class_match.group("name")
            class_indent = len(line) - len(line.lstrip())
            symbols.append(
                Symbol(
                    name=current_class,
                    kind="class",
                    file_path=rel_path,
                    line=line_num,
                )
            )
            continue

        func_match = PY_FUNCTION_RE.match(line)
        if func_match:
            indent = len(func_match.group("indent"))
            name = func_match.group("name")
            return_type = func_match.group("return")
            if return_type:
                return_type = return_type.strip()

            # Skip private/dunder methods
            if name.startswith("_"):
                continue

            if current_class and indent > class_indent:
                symbols.append(
                    Symbol(
                        name=name,
                        kind="method",
                        file_path=rel_path,
                        line=line_num,
                        class_name=current_class,
                        return_type=return_type,
                    )
                )
            else:
                if indent <= class_indent:
                    current_class = None
                    class_indent = -1
                symbols.append(
                    Symbol(
                        name=name,
                        kind="function",
                        file_path=rel_path,
                        line=line_num,
                        return_type=return_type,
                    )
                )

    return symbols


# ---------------------------------------------------------------------------
# Symbol extraction — TypeScript
# ---------------------------------------------------------------------------

TS_FUNCTION_RE = re.compile(
    r"^(?:export\s+)?(?:async\s+)?function\s+(?P<name>[a-zA-Z_$]\w*)\s*"
    r"(?:<[^>]*>)?\s*\((?P<params>[^)]*)\)"
    r"(?:\s*:\s*(?P<return>[^{]+?))?(?:\s*\{)",
    re.MULTILINE,
)
TS_CLASS_RE = re.compile(
    r"^(?:export\s+)?(?:abstract\s+)?class\s+(?P<name>[a-zA-Z_$]\w*)",
    re.MULTILINE,
)
TS_METHOD_RE = re.compile(
    r"^\s+(?:(?:public|private|protected|static|async|readonly)\s+)*"
    r"(?P<name>[a-zA-Z_$]\w*)\s*(?:<[^>]*>)?\s*\((?P<params>[^)]*)\)"
    r"(?:\s*:\s*(?P<return>[^{]+?))?(?:\s*\{)",
    re.MULTILINE,
)


def extract_typescript_symbols(content: str, rel_path: str) -> list[Symbol]:
    """Extract function, class, and method symbols from TypeScript source."""
    symbols: list[Symbol] = []

    for match in TS_FUNCTION_RE.finditer(content):
        name = match.group("name")
        return_type = match.group("return")
        if return_type:
            return_type = return_type.strip()
        line = content[: match.start()].count("\n") + 1
        symbols.append(
            Symbol(
                name=name,
                kind="function",
                file_path=rel_path,
                line=line,
                return_type=return_type,
            )
        )

    current_class: str | None = None
    for match in TS_CLASS_RE.finditer(content):
        current_class = match.group("name")
        line = content[: match.start()].count("\n") + 1
        symbols.append(
            Symbol(
                name=current_class,
                kind="class",
                file_path=rel_path,
                line=line,
            )
        )

    if current_class:
        for match in TS_METHOD_RE.finditer(content):
            name = match.group("name")
            return_type = match.group("return")
            if return_type:
                return_type = return_type.strip()
            line = content[: match.start()].count("\n") + 1
            symbols.append(
                Symbol(
                    name=name,
                    kind="method",
                    file_path=rel_path,
                    line=line,
                    class_name=current_class,
                    return_type=return_type,
                )
            )

    return symbols


# ---------------------------------------------------------------------------
# Symbol collection
# ---------------------------------------------------------------------------


def _should_skip_file(path: Path) -> bool:
    """Check if a file should be skipped."""
    return bool(SKIP_FILE_PATTERNS.search(path.name))


def _is_binary_file(path: Path) -> bool:
    """Quick check for binary content."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        return b"\x00" in chunk
    except (OSError, PermissionError):
        return True


def collect_symbols(repo_root: Path, lang_filter: str | None = None) -> list[Symbol]:
    """Walk repo and extract symbols from supported files."""
    symbols: list[Symbol] = []
    extensions: dict[str, str] = {}

    if lang_filter is None or lang_filter == "python":
        extensions[".py"] = "python"
    if lang_filter is None or lang_filter == "typescript":
        extensions[".ts"] = "typescript"
        extensions[".tsx"] = "typescript"

    for root, dirs, files in os.walk(repo_root, followlinks=False):
        root_path = Path(root)
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]

        for fname in files:
            fpath = root_path / fname
            ext = fpath.suffix
            if ext not in extensions:
                continue
            rel_path = str(fpath.relative_to(repo_root))
            if _should_skip_file(fpath):
                logger.debug("skip pattern: %s", rel_path)
                continue
            if _is_binary_file(fpath):
                logger.debug("skip binary: %s", rel_path)
                continue

            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except (OSError, PermissionError) as exc:
                logger.debug("skip read-error: %s (%s)", rel_path, exc)
                continue

            lang = extensions[ext]
            if lang == "python":
                file_symbols = extract_python_symbols(content, rel_path)
            elif lang == "typescript":
                file_symbols = extract_typescript_symbols(content, rel_path)
            else:
                file_symbols = []
            logger.debug("extracted %d symbols from %s", len(file_symbols), rel_path)
            symbols.extend(file_symbols)

    return symbols


# ---------------------------------------------------------------------------
# Ground truth computation
# ---------------------------------------------------------------------------


def compute_caller_count(repo_root: Path, symbol_name: str) -> int:
    """Count how many files import or reference a symbol by name."""
    t0 = time.perf_counter()
    count = 0
    files_scanned = 0
    pattern = re.compile(r"\b" + re.escape(symbol_name) + r"\b")
    seen_files: set[str] = set()

    for root, dirs, files in os.walk(repo_root, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            if not fname.endswith((".py", ".ts", ".tsx", ".js", ".jsx")):
                continue
            fpath = Path(root) / fname
            rel = str(fpath.relative_to(repo_root))
            if rel in seen_files:
                continue
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except (OSError, PermissionError):
                continue
            files_scanned += 1
            if pattern.search(content):
                seen_files.add(rel)
                count += 1

    # Subtract 1 for the defining file itself
    result = max(0, count - 1)
    elapsed = time.perf_counter() - t0
    logger.debug(
        "caller count for '%s': %d (%.2fs, %d files scanned)",
        symbol_name,
        result,
        elapsed,
        files_scanned,
    )
    return result


def check_module_dependency(repo_root: Path, module_a: str, module_b: str) -> bool:
    """Check if module_a imports from module_b."""
    path_a = _resolve_module_path(repo_root, module_a)
    if path_a is None:
        return False

    try:
        content = path_a.read_text(encoding="utf-8", errors="replace")
    except (OSError, PermissionError):
        return False

    py_patterns = [
        re.compile(r"^\s*(?:from|import)\s+" + re.escape(module_b), re.MULTILINE),
        re.compile(
            r"^\s*from\s+\.\s*" + re.escape(module_b.split(".")[-1]),
            re.MULTILINE,
        ),
    ]
    ts_patterns = [
        re.compile(
            r"""(?:import|require)\s*\(?\s*['"].*""" + re.escape(module_b) + r"""['"]"""
        ),
        re.compile(r"""from\s+['"].*""" + re.escape(module_b) + r"""['"]"""),
    ]

    for pat in py_patterns + ts_patterns:
        if pat.search(content):
            return True
    return False


def _resolve_module_path(repo_root: Path, module_name: str) -> Path | None:
    """Try to resolve a module name to a file path."""
    candidates = [
        repo_root / module_name.replace(".", "/") / "__init__.py",
        repo_root / (module_name.replace(".", "/") + ".py"),
        repo_root / (module_name.replace(".", "/") + ".ts"),
        repo_root / (module_name.replace(".", "/") + ".tsx"),
        repo_root / (module_name + ".py"),
        repo_root / (module_name + ".ts"),
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


# ---------------------------------------------------------------------------
# Template loading
# ---------------------------------------------------------------------------

# Built-in templates (no external file dependency)
BUILTIN_TEMPLATES: dict[str, dict[str, Any]] = {
    "find_function": {
        "category": "probe_navigate",
        "prompt": "What file contains the function `{name}`? Reply with just the relative file path, nothing else.",
        "answer_type": "file_path",
        "capability_tags": ["navigation", "symbol_search"],
        "time_limit_sec": 30,
        "difficulty": "easy",
    },
    "count_callers": {
        "category": "probe_comprehend",
        "prompt": "How many files import or call `{name}`? Reply with just the integer count, nothing else.",
        "answer_type": "integer",
        "capability_tags": ["comprehension", "cross_reference"],
        "time_limit_sec": 30,
        "difficulty": "medium",
    },
    "return_type": {
        "category": "probe_comprehend",
        "prompt": (
            "What type does the method `{class_name}.{method_name}` return?"
            " Reply with just the type annotation string, nothing else."
        ),
        "answer_type": "text",
        "capability_tags": ["comprehension", "type_analysis"],
        "time_limit_sec": 30,
        "difficulty": "medium",
    },
    "module_dependency": {
        "category": "probe_comprehend",
        "prompt": (
            "Does module `{module_a}` depend on (import from) module `{module_b}`?"
            " Reply with just 'yes' or 'no', nothing else."
        ),
        "answer_type": "boolean",
        "capability_tags": ["comprehension", "dependency_analysis"],
        "time_limit_sec": 30,
        "difficulty": "easy",
    },
}


# ---------------------------------------------------------------------------
# Probe generation per template
# ---------------------------------------------------------------------------


def _generate_find_function_probes(
    symbols: list[Symbol],
    templates: dict[str, dict[str, Any]],
    count: int,
) -> list[Probe]:
    """Generate 'find_function' probes."""
    tpl = templates.get("find_function")
    if tpl is None:
        return []

    functions = [s for s in symbols if s.kind == "function"]
    if not functions:
        return []

    selected = random.sample(functions, min(count, len(functions)))
    return [
        Probe(
            template_name="find_function",
            category=tpl["category"],
            prompt=tpl["prompt"].replace("{name}", sym.name),
            answer=sym.file_path,
            answer_type=tpl["answer_type"],
            difficulty=tpl["difficulty"],
            capability_tags=tuple(tpl.get("capability_tags", ())),
            time_limit_sec=tpl.get("time_limit_sec", 30),
        )
        for sym in selected
    ]


def _generate_count_callers_probes(
    symbols: list[Symbol],
    templates: dict[str, dict[str, Any]],
    repo_root: Path,
    count: int,
) -> list[Probe]:
    """Generate 'count_callers' probes."""
    tpl = templates.get("count_callers")
    if tpl is None:
        return []

    functions = [s for s in symbols if s.kind == "function"]
    if not functions:
        return []

    selected = random.sample(functions, min(count, len(functions)))
    return [
        Probe(
            template_name="count_callers",
            category=tpl["category"],
            prompt=tpl["prompt"].replace("{name}", sym.name),
            answer=str(compute_caller_count(repo_root, sym.name)),
            answer_type=tpl["answer_type"],
            difficulty=tpl["difficulty"],
            capability_tags=tuple(tpl.get("capability_tags", ())),
            time_limit_sec=tpl.get("time_limit_sec", 30),
        )
        for sym in selected
    ]


def _generate_return_type_probes(
    symbols: list[Symbol],
    templates: dict[str, dict[str, Any]],
    count: int,
) -> list[Probe]:
    """Generate 'return_type' probes."""
    tpl = templates.get("return_type")
    if tpl is None:
        return []

    methods = [
        s for s in symbols if s.kind == "method" and s.class_name and s.return_type
    ]
    if not methods:
        return []

    selected = random.sample(methods, min(count, len(methods)))
    return [
        Probe(
            template_name="return_type",
            category=tpl["category"],
            prompt=tpl["prompt"]
            .replace("{class_name}", sym.class_name or "")
            .replace("{method_name}", sym.name),
            answer=sym.return_type or "",
            answer_type=tpl["answer_type"],
            difficulty=tpl["difficulty"],
            capability_tags=tuple(tpl.get("capability_tags", ())),
            time_limit_sec=tpl.get("time_limit_sec", 30),
        )
        for sym in selected
    ]


def _generate_module_dependency_probes(
    symbols: list[Symbol],
    templates: dict[str, dict[str, Any]],
    repo_root: Path,
    count: int,
) -> list[Probe]:
    """Generate 'module_dependency' probes."""
    tpl = templates.get("module_dependency")
    if tpl is None:
        return []

    modules: list[str] = sorted(
        {
            str(Path(s.file_path).parent)
            for s in symbols
            if Path(s.file_path).parent != Path(".")
        }
    )

    if len(modules) < 2:
        return []

    probes: list[Probe] = []
    pairs_tried: set[tuple[str, str]] = set()
    attempts = 0
    max_attempts = count * 5

    while len(probes) < count and attempts < max_attempts:
        attempts += 1
        a, b = random.sample(modules, 2)
        pair = (a, b)
        if pair in pairs_tried:
            continue
        pairs_tried.add(pair)

        depends = check_module_dependency(repo_root, a, b)
        answer = "yes" if depends else "no"
        probes.append(
            Probe(
                template_name="module_dependency",
                category=tpl["category"],
                prompt=tpl["prompt"].replace("{module_a}", a).replace("{module_b}", b),
                answer=answer,
                answer_type=tpl["answer_type"],
                difficulty=tpl["difficulty"],
                capability_tags=tuple(tpl.get("capability_tags", ())),
                time_limit_sec=tpl.get("time_limit_sec", 30),
            )
        )

    return probes


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def generate_probes(
    repo_root: Path,
    count: int = DEFAULT_COUNT,
    lang_filter: str | None = None,
    seed: int | None = None,
) -> list[Probe]:
    """Generate a set of micro-benchmark probes for a repository.

    Args:
        repo_root: Path to the repository root.
        count: Desired number of probes (clamped to MIN_PROBES..MAX_PROBES).
        lang_filter: Optional language filter ("python" or "typescript").
        seed: Random seed for reproducibility.

    Returns:
        List of Probe objects. Empty if no symbols found.
    """
    t0 = time.perf_counter()

    if seed is not None:
        random.seed(seed)

    templates = BUILTIN_TEMPLATES
    symbols = collect_symbols(repo_root, lang_filter)

    # Symbol summary (always logged, even when empty, to aid debugging)
    kind_counts = Counter(s.kind for s in symbols)
    file_count = len({s.file_path for s in symbols})
    kind_parts = ", ".join(f"{kind_counts[k]} {k}s" for k in sorted(kind_counts))
    logger.info(
        "Collected %d symbols (%s) from %d files",
        len(symbols),
        kind_parts or "none",
        file_count,
    )

    if not symbols:
        elapsed = time.perf_counter() - t0
        logger.info("Probe generation completed in %.1fs", elapsed)
        return []

    # Distribute count across probe types (roughly equal)
    per_type = max(1, count // 4)
    remainder = count - per_type * 4

    probes: list[Probe] = []
    probes.extend(
        _generate_find_function_probes(symbols, templates, per_type + max(0, remainder))
    )
    probes.extend(
        _generate_count_callers_probes(symbols, templates, repo_root, per_type)
    )
    probes.extend(_generate_return_type_probes(symbols, templates, per_type))
    probes.extend(
        _generate_module_dependency_probes(symbols, templates, repo_root, per_type)
    )

    # Trim to requested count
    if len(probes) > count:
        probes = random.sample(probes, count)

    # Per-template summary
    template_counts = Counter(p.template_name for p in probes)
    template_parts = ", ".join(
        f"{template_counts[t]} {t}" for t in sorted(template_counts)
    )
    logger.info("Generated %s probes", template_parts)

    elapsed = time.perf_counter() - t0
    if elapsed > SLOW_GENERATION_THRESHOLD_SEC:
        logger.warning(
            "Probe generation took %.1fs (>%ds). Consider --lang to filter "
            "or reducing --count.",
            elapsed,
            SLOW_GENERATION_THRESHOLD_SEC,
        )
    logger.info("Probe generation completed in %.1fs", elapsed)

    return probes
