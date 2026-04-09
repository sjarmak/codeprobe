"""Architecture comprehension task generator.

Produces multi-step reasoning tasks that cannot be solved by a single grep.
Uses static analysis of a Python repository: import graph, call graph
approximation, and symbol extraction (reused from ``probe/generator``).

Task templates (all ``task_type="architecture_comprehension"``,
``verification_mode="artifact_eval"``):

1. ``import_chain`` -- "List all files that transitively import module X"
2. ``dependency_analysis`` -- "Which modules need to change if function X in
   module Y changed its signature?"
3. ``return_type_resolution`` -- "What is the return type annotation of the
   function called by Class.method()?"
4. ``transitive_dependency`` -- "Does module A transitively depend on B?"

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
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codeprobe.mining._graph import (
    _RepoIndex,
    _answer_files_beat_grep,
    _build_index,
    _call_regex,
    _reachable_modules,
    _shortest_path_length,
    _single_grep_importers,
    _transitive_importers,
)
from codeprobe.mining.writer import logger as _writer_logger  # noqa: F401
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

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


# Spec registry -- populated by generate(), consumed by write_comprehension_tasks().
# Prefer passing specs explicitly via generate()'s return value and
# write_comprehension_tasks(specs=...) parameter. The module-level dict is
# kept as a fallback for backwards compatibility but may be removed.
_TASK_SPECS: dict[str, ComprehensionTaskSpec] = {}


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
            direct = self._index.rgraph.get(module, set())
            indirect = transitive - direct
            if not indirect:
                continue  # no transitive hop => trivially grepable
            # Convert module set -> file set
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
        typed_functions: dict[str, tuple] = {}
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

        # Compute reachability once per module, reuse for both true and false.
        true_candidates: list[tuple[str, str]] = []
        false_candidates: list[tuple[str, str]] = []
        enough_true = want_true * 3
        enough_false = want_false * 3
        for a in modules:
            if (
                len(true_candidates) >= enough_true
                and len(false_candidates) >= enough_false
            ):
                break
            reachable = _reachable_modules(self._index.graph, a)
            # True cases: chain length >= 2 (requires traversal)
            if len(true_candidates) < enough_true:
                for b in reachable:
                    path_len = _shortest_path_length(self._index.graph, a, b)
                    if path_len is not None and path_len >= 2:
                        true_candidates.append((a, b))
            # False cases: b not reachable from a
            if len(false_candidates) < enough_false:
                for b in modules:
                    if b == a or b in reachable:
                        continue
                    false_candidates.append((a, b))
                    if len(false_candidates) >= enough_false:
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
            command="python3 -m codeprobe.core.scoring --artifact .",
            verification_mode="artifact_eval",
            eval_command="",
            ground_truth_path="tests/ground_truth.json",
            answer_schema=spec.answer_type,
            reward_type="artifact",
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
# Backward-compatible re-exports
# ---------------------------------------------------------------------------
# Tests and other modules import graph/writer functions directly from this
# module. Re-export them so existing imports continue to work.

from codeprobe.mining._graph import (  # noqa: E402, F811
    _RepoIndex as _RepoIndex,
    _answer_files_beat_grep as _answer_files_beat_grep,
    _build_index as _build_index,
    _reachable_modules as _reachable_modules,
    _shortest_path_length as _shortest_path_length,
    _single_grep_importers as _single_grep_importers,
    _transitive_importers as _transitive_importers,
)
from codeprobe.mining.comprehension_writer import (  # noqa: E402, F811
    write_comprehension_tasks as write_comprehension_tasks,
)
