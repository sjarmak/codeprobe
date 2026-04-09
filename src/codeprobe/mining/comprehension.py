"""Architecture comprehension task generator.

Produces multi-step reasoning tasks that cannot be solved by a single grep.
Uses static analysis of a Python repository: import graph, call graph
approximation, and symbol extraction (reused from ``probe/generator``).

Task templates (all ``task_type="architecture_comprehension"``,
``verification_mode="artifact_eval"``):

1. ``import_chain`` — "List all files that transitively import module X"
2. ``dependency_analysis`` — "Which modules need to change if function X in
   module Y changed its signature?"
3. ``return_type_resolution`` — "What is the return type annotation of the
   function called by Class.method()?"
4. ``transitive_dependency`` — "Does module A transitively depend on B?"

Ground truth format (new):

    {
      "answer": ...,
      "answer_type": "file_list" | "count" | "boolean" | "text",
      "confidence": 0.95,
      "provenance": "deterministic"
    }

Discrimination gate: for ``file_list`` answers, tasks are rejected if the
answer set is identical to what a single ``grep -l "import module"`` would
return. This ensures the task actually requires transitive traversal.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from codeprobe.mining.writer import logger as _writer_logger  # noqa: F401
from codeprobe.models.task import Task, TaskMetadata, TaskVerification
from codeprobe.probe.generator import (
    SKIP_DIRS,
    Symbol,
    extract_python_symbols,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------

_AnswerValue = list[str] | bool | str | int


@dataclass(frozen=True)
class ComprehensionTaskSpec:
    """Internal representation of a single comprehension task."""

    template: str
    question: str
    answer: _AnswerValue
    answer_type: str  # file_list | count | boolean | text
    target: str
    confidence: float = 0.95
    provenance: str = "deterministic"
    metadata: dict[str, Any] = field(default_factory=dict)


# Module-level registry so the writer can look up ground truth for a task
# without mutating the frozen Task dataclass. Populated by
# ``ComprehensionGenerator.generate`` and consumed by
# ``write_comprehension_tasks``.
_TASK_SPECS: dict[str, ComprehensionTaskSpec] = {}


# ---------------------------------------------------------------------------
# Import-graph construction
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
    # If the current module has no explicit package, resolving from it is
    # ambiguous; we still try to combine.
    combined_parts = [*base]
    if tail:
        combined_parts.extend(tail.split("."))
    combined = ".".join(p for p in combined_parts if p)
    if not combined:
        return None
    # Resolve to the closest known package/module.
    return combined


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
            # If two files collapse to the same module (package/__init__ vs
            # module.py), prefer the package form.
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
            # Also consider each imported name as a potential submodule:
            # ``from pkg import b`` -> try ``pkg.b``.
            names_blob = m.group("names")
            # Strip parenthesised lists, trailing backslash continuations.
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


def _resolve_import_target(raw: str, known_modules: set[str]) -> str | None:
    """Best-effort match of a raw import target to an internal module.

    Tries the full dotted name, then progressively shorter prefixes. Also
    tries each known module that is a prefix of ``raw``.
    """
    if raw in known_modules:
        return raw
    # Longest-prefix match: e.g. ``from codeprobe.models.task import Task``
    # resolves to ``codeprobe.models.task``. If Task is a submodule we catch
    # it below.
    parts = raw.split(".")
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in known_modules:
            return candidate
    return None


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
    # Answer must include files not reachable by the grep baseline.
    return bool(answer_files - grep_set)


# ---------------------------------------------------------------------------
# Generator
# ---------------------------------------------------------------------------


class ComprehensionGenerator:
    """Generate architecture comprehension tasks from a Python repository."""

    def __init__(self, repo_path: Path) -> None:
        self.repo_path = Path(repo_path).resolve()
        if not self.repo_path.is_dir():
            raise ValueError(f"repo_path is not a directory: {repo_path}")
        self._index = _build_index(self.repo_path)
        self._specs: list[ComprehensionTaskSpec] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, count: int = 10) -> list[Task]:
        """Produce up to ``count`` tasks across all four templates."""
        if count <= 0:
            return []

        per_template = max(1, count // 4)
        # Give import_chain any remainder so we get the requested total.
        remainder = count - per_template * 4

        specs: list[ComprehensionTaskSpec] = []
        specs.extend(self._generate_import_chain(per_template + max(0, remainder)))
        specs.extend(self._generate_dependency_analysis(per_template))
        specs.extend(self._generate_return_type_resolution(per_template))
        specs.extend(self._generate_transitive_dependency(per_template))

        # Deduplicate by (template, target)
        unique: dict[tuple[str, str], ComprehensionTaskSpec] = {}
        for spec in specs:
            unique.setdefault((spec.template, spec.target), spec)
        specs = list(unique.values())[:count]

        self._specs = specs
        tasks: list[Task] = []
        for idx, spec in enumerate(specs):
            task = self._spec_to_task(spec, idx)
            tasks.append(task)
            _TASK_SPECS[task.id] = spec
        logger.info(
            "ComprehensionGenerator produced %d tasks (requested %d)",
            len(tasks),
            count,
        )
        return tasks

    # ------------------------------------------------------------------
    # Template: import_chain
    # ------------------------------------------------------------------

    def _generate_import_chain(self, count: int) -> list[ComprehensionTaskSpec]:
        """Modules with >= 1 indirect importer whose answer beats single-grep."""
        if count <= 0:
            return []
        candidates: list[tuple[str, set[str]]] = []
        for module in sorted(self._index.module_to_file.keys()):
            transitive = _transitive_importers(self._index.rgraph, module)
            indirect = _indirect_importers(self._index.rgraph, module)
            if not indirect:
                continue  # no transitive hop => trivially grepable
            # Convert module set → file set
            answer_files = {
                self._index.module_to_file[m]
                for m in transitive
                if m in self._index.module_to_file
            }
            if not answer_files:
                continue
            if not _answer_files_beat_grep(self._index, module, answer_files):
                continue
            candidates.append((module, answer_files))

        candidates.sort(key=lambda kv: (-len(kv[1]), kv[0]))
        out: list[ComprehensionTaskSpec] = []
        for module, files in candidates[:count]:
            sorted_files = sorted(files)
            out.append(
                ComprehensionTaskSpec(
                    template="import_chain",
                    question=(
                        f"List every file in this repository that transitively "
                        f"imports the module `{module}` (directly or through a "
                        f"chain of imports). Return a JSON array of file paths "
                        f"relative to the repository root, sorted lexicographically."
                    ),
                    answer=sorted_files,
                    answer_type="file_list",
                    target=module,
                    metadata={
                        "target_file": self._index.module_to_file.get(module, ""),
                    },
                )
            )
        return out

    # ------------------------------------------------------------------
    # Template: dependency_analysis
    # ------------------------------------------------------------------

    def _generate_dependency_analysis(self, count: int) -> list[ComprehensionTaskSpec]:
        """Find callers of a top-level function via import+call heuristic."""
        if count <= 0:
            return []
        candidates: list[ComprehensionTaskSpec] = []

        for rel, symbols in self._index.symbols.items():
            module = self._index.file_to_module.get(rel)
            if module is None:
                continue
            for sym in symbols:
                if sym.kind != "function":
                    continue
                importers = self._index.rgraph.get(module, set())
                if not importers:
                    continue
                caller_files: set[str] = set()
                call_pat = _call_regex(sym.name)
                for importer_mod in importers:
                    importer_file = self._index.module_to_file.get(importer_mod)
                    if importer_file is None:
                        continue
                    content = self._index.sources.get(importer_file, "")
                    if call_pat.search(content):
                        caller_files.add(importer_file)
                if not caller_files:
                    continue
                # Discrimination gate: answer must differ from a naive
                # `grep -l "funcname("` across the repo.
                naive = self._grep_call_sites(sym.name, exclude=rel)
                if caller_files == naive:
                    # If it equals the grep result BUT the grep result was
                    # filtered by the import requirement, still accept when
                    # the task added value by pruning false positives.
                    # Strict rule: require non-equality.
                    continue
                candidates.append(
                    ComprehensionTaskSpec(
                        template="dependency_analysis",
                        question=(
                            f"The function `{sym.name}` is defined in module "
                            f"`{module}` (file: `{rel}`). If its signature "
                            f"changed, which files in this repository would "
                            f"need to be updated? Return a JSON array of "
                            f"file paths relative to the repository root, "
                            f"sorted lexicographically. Only include files "
                            f"that both import `{module}` AND actually call "
                            f"`{sym.name}`."
                        ),
                        answer=sorted(caller_files),
                        answer_type="file_list",
                        target=f"{module}.{sym.name}",
                        metadata={"defined_in": rel},
                    )
                )
                if len(candidates) >= count * 3:
                    break
            if len(candidates) >= count * 3:
                break

        # Sort by number of callers (larger answers more interesting).
        candidates.sort(
            key=lambda s: (
                -len(s.answer) if isinstance(s.answer, list) else 0,
                s.target,
            )
        )
        return candidates[:count]

    def _grep_call_sites(self, name: str, exclude: str) -> set[str]:
        """Files containing a literal ``name(`` call, excluding one file."""
        pat = _call_regex(name)
        hits: set[str] = set()
        for rel, content in self._index.sources.items():
            if rel == exclude:
                continue
            if pat.search(content):
                hits.add(rel)
        return hits

    # ------------------------------------------------------------------
    # Template: return_type_resolution
    # ------------------------------------------------------------------

    def _generate_return_type_resolution(
        self, count: int
    ) -> list[ComprehensionTaskSpec]:
        """Pick a method that calls a cross-file function with a known return type."""
        if count <= 0:
            return []

        # Build an index of top-level functions with return types.
        typed_functions: dict[str, tuple[Symbol, str]] = {}
        for rel, symbols in self._index.symbols.items():
            for sym in symbols:
                if (
                    sym.kind == "function"
                    and sym.return_type
                    and sym.name not in typed_functions
                ):
                    typed_functions[sym.name] = (sym, rel)

        if not typed_functions:
            return []

        out: list[ComprehensionTaskSpec] = []
        for rel, symbols in self._index.symbols.items():
            if len(out) >= count:
                break
            # Group methods by class
            file_lines = self._index.sources.get(rel, "").splitlines()
            for sym in symbols:
                if sym.kind != "method":
                    continue
                # Approximate method body = next 40 lines after declaration
                start = sym.line
                body = "\n".join(file_lines[start : start + 40])
                for target_name, (target_sym, target_rel) in typed_functions.items():
                    if target_rel == rel:
                        continue  # must be cross-file for multi-step reasoning
                    if _call_regex(target_name).search(body):
                        answer_str = (target_sym.return_type or "").strip()
                        if not answer_str:
                            continue
                        class_name = sym.class_name or ""
                        out.append(
                            ComprehensionTaskSpec(
                                template="return_type_resolution",
                                question=(
                                    f"In file `{rel}`, the method "
                                    f"`{class_name}.{sym.name}` calls a "
                                    f"top-level function defined in another "
                                    f"file. What is the return type "
                                    f"annotation of that called function? "
                                    f"Return only the type annotation "
                                    f"string, exactly as written in the "
                                    f"source."
                                ),
                                answer=answer_str,
                                answer_type="text",
                                target=f"{rel}::{class_name}.{sym.name}",
                                metadata={
                                    "called_function": target_name,
                                    "called_from_file": target_rel,
                                },
                            )
                        )
                        break
                if len(out) >= count:
                    break
        return out

    # ------------------------------------------------------------------
    # Template: transitive_dependency
    # ------------------------------------------------------------------

    def _generate_transitive_dependency(
        self, count: int
    ) -> list[ComprehensionTaskSpec]:
        if count <= 0:
            return []

        modules = sorted(self._index.module_to_file.keys())
        if len(modules) < 2:
            return []

        out: list[ComprehensionTaskSpec] = []
        want_true = max(1, count // 2)
        want_false = count - want_true

        # True cases: chain length >= 2 (requires traversal)
        true_candidates: list[tuple[str, str]] = []
        for a in modules:
            reachable = _reachable_modules(self._index.graph, a)
            for b in reachable:
                path_len = _shortest_path_length(self._index.graph, a, b)
                if path_len is not None and path_len >= 2:
                    true_candidates.append((a, b))
            if len(true_candidates) >= want_true * 3:
                break

        # False cases: b not reachable from a
        false_candidates: list[tuple[str, str]] = []
        for a in modules:
            reachable = _reachable_modules(self._index.graph, a)
            for b in modules:
                if b == a or b in reachable:
                    continue
                false_candidates.append((a, b))
                if len(false_candidates) >= want_false * 3:
                    break
            if len(false_candidates) >= want_false * 3:
                break

        for a, b in true_candidates[:want_true]:
            out.append(
                ComprehensionTaskSpec(
                    template="transitive_dependency",
                    question=(
                        f"Does module `{a}` transitively depend on module "
                        f"`{b}`? A transitive dependency means `{a}` imports "
                        f"`{b}` directly or through a chain of intermediate "
                        f"modules. Answer with the single word `true` or "
                        f"`false`."
                    ),
                    answer=True,
                    answer_type="boolean",
                    target=f"{a}->{b}",
                )
            )
        for a, b in false_candidates[:want_false]:
            out.append(
                ComprehensionTaskSpec(
                    template="transitive_dependency",
                    question=(
                        f"Does module `{a}` transitively depend on module "
                        f"`{b}`? A transitive dependency means `{a}` imports "
                        f"`{b}` directly or through a chain of intermediate "
                        f"modules. Answer with the single word `true` or "
                        f"`false`."
                    ),
                    answer=False,
                    answer_type="boolean",
                    target=f"{a}->{b}",
                )
            )
        return out

    # ------------------------------------------------------------------
    # Task construction
    # ------------------------------------------------------------------

    def _spec_to_task(self, spec: ComprehensionTaskSpec, idx: int) -> Task:
        digest = hashlib.sha1(
            f"{spec.template}|{spec.target}|{idx}".encode()
        ).hexdigest()[:8]
        task_id = f"comprehension-{spec.template}-{idx:03d}-{digest}"
        metadata = TaskMetadata(
            name=f"{spec.template}: {spec.target}",
            difficulty="hard",
            description=spec.question,
            language="python",
            category="architecture_comprehension",
            task_type="architecture_comprehension",
            tags=("comprehension", spec.template),
            enrichment_source="static_analysis",
        )
        verification = TaskVerification(
            type="artifact_eval",
            command="bash tests/test.sh",
            verification_mode="artifact_eval",
            eval_command="",
            ground_truth_path="tests/ground_truth.json",
            answer_schema=spec.answer_type,
            reward_type="binary",
            ground_truth_schema_version="comprehension-v1",
        )
        return Task(
            id=task_id,
            repo=self.repo_path.name,
            metadata=metadata,
            verification=verification,
            instruction_path="instruction.md",
            time_limit_sec=600,
            verification_modes=("artifact_eval",),
        )


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def write_comprehension_tasks(tasks: list[Task], output_dir: Path) -> list[Path]:
    """Write comprehension tasks to disk with the new ground_truth format.

    Produces::

        output_dir/<task.id>/
            instruction.md
            metadata.json
            tests/ground_truth.json

    Ground truth JSON::

        {
          "answer": ...,
          "answer_type": "file_list" | "count" | "boolean" | "text",
          "confidence": 0.95,
          "provenance": "deterministic"
        }

    Tasks must have been produced by ``ComprehensionGenerator.generate`` —
    the spec is looked up from a process-wide registry keyed on ``task.id``.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for task in tasks:
        spec = _TASK_SPECS.get(task.id)
        if spec is None:
            logger.warning("No spec registered for task %s, skipping", task.id)
            continue

        safe_id = Path(task.id).name
        if not safe_id or safe_id != task.id:
            raise ValueError(f"Invalid task id for filesystem use: {task.id!r}")

        task_dir = output_dir / safe_id
        tests_dir = task_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)

        instruction = _build_instruction(task, spec)
        (task_dir / "instruction.md").write_text(instruction, encoding="utf-8")

        metadata_payload = asdict(task)
        (task_dir / "metadata.json").write_text(
            json.dumps(metadata_payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        ground_truth = {
            "answer": spec.answer,
            "answer_type": spec.answer_type,
            "confidence": spec.confidence,
            "provenance": spec.provenance,
        }
        (tests_dir / "ground_truth.json").write_text(
            json.dumps(ground_truth, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )

        written.append(task_dir)
        logger.info("Wrote comprehension task %s -> %s", task.id, task_dir)

    return written


def _build_instruction(task: Task, spec: ComprehensionTaskSpec) -> str:
    """Render instruction.md for a comprehension task."""
    answer_format = {
        "file_list": (
            "Return a JSON array of file paths (strings) relative to the "
            "repository root, sorted lexicographically."
        ),
        "boolean": "Answer with the single word `true` or `false`.",
        "text": "Return only the exact text, with no extra commentary.",
        "count": "Return only a single integer.",
    }.get(spec.answer_type, "Provide your answer.")

    return (
        f"# {task.metadata.name}\n\n"
        f"**Repository:** {task.repo}\n"
        f"**Task type:** architecture_comprehension\n"
        f"**Template:** {spec.template}\n\n"
        "## Question\n\n"
        f"{spec.question}\n\n"
        "## Answer Format\n\n"
        f"{answer_format}\n\n"
        "Write your answer to `answer.txt` in the repository root.\n"
    )
