"""Import-graph construction and traversal for Python repositories.

Builds a static index (_RepoIndex) of a Python repository: module mapping,
import graph, reverse graph, source texts, and extracted symbols. Provides
BFS-based traversals for transitive importers, reachable modules, and shortest
path computation. Also includes a discrimination gate that rejects tasks whose
answers are trivially reproducible by a single grep.
"""

from __future__ import annotations

import os
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from codeprobe.probe.generator import (
    SKIP_DIRS,
    Symbol,
    extract_python_symbols,
)

# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------

_IMPORT_RE = re.compile(r"^\s*import\s+([\w\.]+)", re.MULTILINE)
_FROM_RE = re.compile(
    r"^\s*from\s+(\.*)([\w\.]*)\s+import\s+(?P<names>[^\n#]+)",
    re.MULTILINE,
)
_NAME_RE = re.compile(r"[A-Za-z_][\w]*")
_CALL_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _call_regex(name: str) -> re.Pattern[str]:
    """Cached compiled regex for detecting `name(` call sites."""
    pat = _CALL_RE_CACHE.get(name)
    if pat is None:
        pat = re.compile(r"\b" + re.escape(name) + r"\s*\(")
        _CALL_RE_CACHE[name] = pat
    return pat


# ---------------------------------------------------------------------------
# Path / module helpers
# ---------------------------------------------------------------------------


def _path_to_module(rel_path: str) -> str:
    """Convert a relative .py path to a dotted module name.

    Strips a leading ``src/`` segment if present and drops trailing
    ``__init__`` so packages resolve to their directory module name.
    """
    parts = Path(rel_path).with_suffix("").parts
    if parts and parts[0] == "src":
        parts = parts[1:]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _resolve_relative(
    current_module: str, dots: int, tail: str, package_modules: set[str]
) -> str | None:
    """Resolve a relative import to an absolute module name."""
    if dots == 0:
        return tail or None
    parts = current_module.split(".")
    # dots=1 means current package, dots=2 means parent, etc.
    up = dots - 1
    if up >= len(parts):
        return None
    base = parts[: len(parts) - up - 1] if (len(parts) - up - 1) >= 0 else []
    combined_parts = [*base]
    if tail:
        combined_parts.extend(tail.split("."))
    combined = ".".join(p for p in combined_parts if p)
    if not combined:
        return None
    return combined


def _resolve_import_target(raw: str, known_modules: set[str]) -> str | None:
    """Best-effort match of a raw import target to an internal module.

    Tries the full dotted name, then progressively shorter prefixes.
    """
    if raw in known_modules:
        return raw
    parts = raw.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in known_modules:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Repo index
# ---------------------------------------------------------------------------


@dataclass
class _RepoIndex:
    """Flattened static index of a Python repo."""

    # module_name -> relative file path
    module_to_file: dict[str, str]
    # relative file path -> module_name
    file_to_module: dict[str, str]
    # module_name -> set of module_names it imports (internal only)
    graph: dict[str, set[str]]
    # reverse: module_name -> set of module_names that import it
    rgraph: dict[str, set[str]]
    # relative file path -> raw source text
    sources: dict[str, str]
    # relative file path -> extracted symbols
    symbols: dict[str, list[Symbol]]


def _build_index(repo_path: Path) -> _RepoIndex:
    """Walk ``repo_path`` and build an internal import graph + symbol map."""
    module_to_file: dict[str, str] = {}
    file_to_module: dict[str, str] = {}
    sources: dict[str, str] = {}
    symbols_map: dict[str, list[Symbol]] = {}

    # Pass 1: collect files & modules
    for root, dirs, files in os.walk(repo_path, followlinks=False):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = Path(root) / fname
            try:
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except (OSError, PermissionError):
                continue
            rel = str(fpath.relative_to(repo_path))
            mod = _path_to_module(rel)
            if not mod:
                continue
            if mod in module_to_file:
                existing = module_to_file[mod]
                if "__init__" in rel and "__init__" not in existing:
                    module_to_file[mod] = rel
                    file_to_module.pop(existing, None)
                    file_to_module[rel] = mod
                else:
                    file_to_module[rel] = mod
                    continue
            else:
                module_to_file[mod] = rel
                file_to_module[rel] = mod
            sources[rel] = content
            symbols_map[rel] = extract_python_symbols(content, rel)

    known_modules = set(module_to_file.keys())

    # Pass 2: build graph
    graph: dict[str, set[str]] = {m: set() for m in known_modules}
    rgraph: dict[str, set[str]] = {m: set() for m in known_modules}

    for rel, content in sources.items():
        current_mod = file_to_module.get(rel)
        if current_mod is None:
            continue

        raw_targets: set[str] = set()
        for m in _IMPORT_RE.finditer(content):
            raw_targets.add(m.group(1))
        for m in _FROM_RE.finditer(content):
            dots = len(m.group(1))
            tail = m.group(2)
            base = _resolve_relative(current_mod, dots, tail, known_modules)
            if base:
                raw_targets.add(base)
            names_blob = m.group("names")
            names_blob = names_blob.replace("(", " ").replace(")", " ")
            for name_match in _NAME_RE.finditer(names_blob):
                name = name_match.group(0)
                if name in {"as", "import"}:
                    continue
                if base:
                    raw_targets.add(f"{base}.{name}")

        for target in raw_targets:
            resolved_mod = _resolve_import_target(target, known_modules)
            if resolved_mod and resolved_mod != current_mod:
                graph[current_mod].add(resolved_mod)
                rgraph[resolved_mod].add(current_mod)

    return _RepoIndex(
        module_to_file=module_to_file,
        file_to_module=file_to_module,
        graph=graph,
        rgraph=rgraph,
        sources=sources,
        symbols=symbols_map,
    )


# ---------------------------------------------------------------------------
# Graph traversals
# ---------------------------------------------------------------------------


def _transitive_importers(rgraph: dict[str, set[str]], target: str) -> set[str]:
    """Return all modules that can reach ``target`` via the import graph.

    Excludes ``target`` itself. Includes both direct and indirect importers.
    """
    seen: set[str] = set()
    queue: deque[str] = deque(rgraph.get(target, set()))
    while queue:
        mod = queue.popleft()
        if mod in seen:
            continue
        seen.add(mod)
        for parent in rgraph.get(mod, set()):
            if parent not in seen:
                queue.append(parent)
    return seen


def _indirect_importers(rgraph: dict[str, set[str]], target: str) -> set[str]:
    """Transitive importers that are NOT direct importers of ``target``."""
    all_t = _transitive_importers(rgraph, target)
    direct = rgraph.get(target, set())
    return all_t - direct


def _reachable_modules(graph: dict[str, set[str]], start: str) -> set[str]:
    """All modules reachable from ``start`` (excluding ``start`` itself)."""
    seen: set[str] = set()
    queue: deque[str] = deque(graph.get(start, set()))
    while queue:
        mod = queue.popleft()
        if mod in seen:
            continue
        seen.add(mod)
        for child in graph.get(mod, set()):
            if child not in seen:
                queue.append(child)
    return seen


def _shortest_path_length(
    graph: dict[str, set[str]], start: str, goal: str
) -> int | None:
    """BFS shortest path length from ``start`` to ``goal``; ``None`` if unreachable."""
    if start == goal:
        return 0
    seen: set[str] = {start}
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    while queue:
        node, dist = queue.popleft()
        for child in graph.get(node, set()):
            if child == goal:
                return dist + 1
            if child not in seen:
                seen.add(child)
                queue.append((child, dist + 1))
    return None


# ---------------------------------------------------------------------------
# Discrimination gate
# ---------------------------------------------------------------------------


def _single_grep_importers(index: _RepoIndex, target_module: str) -> set[str]:
    """Files that would be found by a single grep for ``import <target>``.

    Simulates ``grep -l "import target"`` + ``grep -l "from target"`` over
    the repo. Used as the baseline to reject trivially-grepable tasks.
    """
    last = target_module.split(".")[-1]
    patterns = [
        re.compile(r"^\s*import\s+" + re.escape(target_module) + r"\b", re.MULTILINE),
        re.compile(r"^\s*from\s+" + re.escape(target_module) + r"\b", re.MULTILINE),
        re.compile(r"^\s*import\s+.*\b" + re.escape(last) + r"\b", re.MULTILINE),
        re.compile(r"^\s*from\s+.*\b" + re.escape(last) + r"\b", re.MULTILINE),
    ]
    hits: set[str] = set()
    target_file = index.module_to_file.get(target_module)
    for rel, content in index.sources.items():
        if rel == target_file:
            continue
        for pat in patterns:
            if pat.search(content):
                hits.add(rel)
                break
    return hits


def _answer_files_beat_grep(
    index: _RepoIndex, target_module: str, answer_files: set[str]
) -> bool:
    """Return True iff the answer set cannot be produced by a single grep.

    The gate passes if the answer contains at least one file that a
    single-grep for the target module would NOT find. This guarantees the
    task requires transitive reasoning.
    """
    grep_set = _single_grep_importers(index, target_module)
    return bool(answer_files - grep_set)
