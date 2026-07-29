"""Independent AST rederivation + consensus for comprehension tasks.

The comprehension generator (:mod:`codeprobe.mining.comprehension`) derives
every answer from a regex-built import graph (:mod:`codeprobe.mining._graph`):
multiline regexes over raw source text for import edges, ``name(`` byte
patterns for call sites, and fixed 40-line windows for method bodies. This
module re-derives each answer through a genuinely independent path — Python's
own parser (:mod:`ast`): import edges from ``ast.Import``/``ast.ImportFrom``
nodes (with structural ``level`` for relative imports), call sites from
``ast.Call`` nodes inside the *actual* method body node, and return
annotations from ``ast.get_source_segment`` of the ``returns`` node.

Shared surface between the two backends is limited to the question
vocabulary: the file walk (``SKIP_DIRS`` + ``.py``) and the path→module
naming convention (``_path_to_module``). Everything answer-bearing — which
imports exist, which calls occur, what an annotation says — is derived
independently, so divergences expose real ground-truth defects (imports
inside docstrings, comment-only "calls", ambiguous return-type questions).

Consensus policy: exact answer equality. A task ships only when both
backends produced the identical answer; anything else is quarantined with a
``consensus.v1`` divergence report recording both derivations. The report
uses the exact field names the aoa-bench loader deserializes
(``decision``, ``backend_results[].{backend, available, files, error}``) so
a shipped comprehension task classifies ``NativeComposed``.

Two templates carry template-specific validity on top of answer equality,
because their oracle CHAIN (the files aoa's trace-locality metric compares
agent navigation against) must itself be backend-agreed:

* ``transitive_dependency`` (true answers): each backend derives its own
  shortest witness chain a..b over its own graph; the SHIPPED witness is one
  shortest path over the edge-INTERSECTION of the two graphs, so both
  backends certify every hop. Witnesses need not be identical paths — only
  the intersection witness must exist. If both backends agree on
  reachability but only via backend-exclusive edges (docstring imports the
  regex sees, parenthesized multiline imports only the AST sees), there is
  no both-backend witness and the task is quarantined: the boolean answer
  key would be fine, but the oracle chain would not be well-defined. False
  answers have no chain; the endpoints reach the oracle via ``symbol``.
  The full witness (endpoints included, in chain order) lands in
  ``consensus_files``.
* ``return_type_resolution``: ``defining_file`` is the file where the
  resolved symbol (the called function whose annotation is the answer) is
  actually defined. The generator's choice must be among the AST backend's
  defining-file candidates for the same annotation, else the task is
  quarantined — matching annotation text from a different file means the
  backends disagree about where the answer lives.

Neither the witness chain nor ``defining_file`` is ever printed into the
agent-visible ``instruction.md``; they live only in the held-out
``divergence_report.json``.

ZFC compliance: pure mechanism — parser walks, graph traversals, and exact
equality checks. No semantic judgment, no calibration knobs.
"""

from __future__ import annotations

import ast
import logging
import os
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from codeprobe.mining._graph import _path_to_module
from codeprobe.mining.comprehension import ComprehensionTaskSpec
from codeprobe.probe.generator import SKIP_DIRS

logger = logging.getLogger(__name__)

# Backend identities recorded in divergence_report.json. Distinct names are
# what makes the task NativeComposed downstream — never alias them.
GENERATOR_BACKEND = "regex_import_graph"
AST_BACKEND = "python_ast_graph"

_ANSWER = list[str] | bool | str | int | None


# ---------------------------------------------------------------------------
# AST index
# ---------------------------------------------------------------------------


@dataclass
class AstIndex:
    """Import graph and parse trees derived from real ``ast`` parses."""

    # module_name -> canonical relative file path
    module_to_file: dict[str, str]
    # relative file path -> module_name (canonical files only)
    file_to_module: dict[str, str]
    # module -> set of internal modules it imports
    graph: dict[str, set[str]]
    # module -> set of internal modules importing it
    rgraph: dict[str, set[str]]
    # relative file path -> parsed tree (canonical, parseable files only)
    trees: dict[str, ast.Module]
    # relative file path -> raw source (for ast.get_source_segment)
    sources: dict[str, str]
    # relative file path -> parse error (files ast could not parse)
    parse_failures: dict[str, str] = field(default_factory=dict)


def build_ast_index(repo_path: Path) -> AstIndex:
    """Walk *repo_path* and build an import graph from ``ast`` parses.

    The walk and the path→module naming match the generator's conventions
    (same question vocabulary); edge extraction is fully AST-derived.
    Files that fail to parse are recorded in ``parse_failures`` — their
    absence from the graph is a legitimate divergence source, because the
    regex backend still reads them.
    """
    repo_path = Path(repo_path).resolve()
    module_to_file: dict[str, str] = {}
    sources: dict[str, str] = {}

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
                # Same canonicalization rule as the generator: a package
                # __init__ wins over a same-named module file.
                if "__init__" in rel and "__init__" not in existing:
                    module_to_file[mod] = rel
                    sources.pop(existing, None)
                    sources[rel] = content
                continue
            module_to_file[mod] = rel
            sources[rel] = content

    file_to_module = {rel: mod for mod, rel in module_to_file.items()}
    known_modules = set(module_to_file)

    trees: dict[str, ast.Module] = {}
    parse_failures: dict[str, str] = {}
    for rel, content in sources.items():
        try:
            trees[rel] = ast.parse(content)
        except (SyntaxError, ValueError) as exc:
            parse_failures[rel] = f"{type(exc).__name__}: {exc}"

    graph: dict[str, set[str]] = {m: set() for m in known_modules}
    rgraph: dict[str, set[str]] = {m: set() for m in known_modules}
    for rel, tree in trees.items():
        current_mod = file_to_module[rel]
        is_package = Path(rel).stem == "__init__"
        for target in _import_targets(tree, current_mod, is_package):
            resolved = _resolve_prefix(target, known_modules)
            if resolved and resolved != current_mod:
                graph[current_mod].add(resolved)
                rgraph[resolved].add(current_mod)

    return AstIndex(
        module_to_file=module_to_file,
        file_to_module=file_to_module,
        graph=graph,
        rgraph=rgraph,
        trees=trees,
        sources=sources,
        parse_failures=parse_failures,
    )


def _import_targets(
    tree: ast.Module, current_mod: str, is_package: bool
) -> set[str]:
    """Raw dotted import targets of one module, from AST nodes only."""
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                targets.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative_base(
                current_mod, is_package, node.level, node.module
            )
            if base is None:
                continue
            targets.add(base)
            # ``from pkg import sub`` may import a submodule.
            for alias in node.names:
                targets.add(f"{base}.{alias.name}")
    targets.discard(current_mod)
    return targets


def _resolve_relative_base(
    current_mod: str, is_package: bool, level: int, module: str | None
) -> str | None:
    """Resolve an ``ImportFrom`` base per real Python relative-import rules.

    ``level=1`` is the current package (the module itself for a package
    ``__init__``); each extra level goes up one package. Beyond the top of
    the package tree resolves to ``None``.
    """
    if level == 0:
        return module
    parts = current_mod.split(".") if current_mod else []
    pkg_parts = parts if is_package else parts[:-1]
    up = level - 1
    if up > len(pkg_parts):
        return None
    base_parts = pkg_parts[: len(pkg_parts) - up]
    base = ".".join(base_parts)
    if module:
        base = f"{base}.{module}" if base else module
    return base or None


def _resolve_prefix(raw: str, known_modules: set[str]) -> str | None:
    """Longest known-module prefix of a raw dotted import target."""
    if raw in known_modules:
        return raw
    parts = raw.split(".")
    for i in range(len(parts) - 1, 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in known_modules:
            return candidate
    return None


# ---------------------------------------------------------------------------
# Graph traversals (over the AST-derived edges)
# ---------------------------------------------------------------------------


def _transitive_importers(rgraph: dict[str, set[str]], target: str) -> set[str]:
    seen: set[str] = set()
    queue: deque[str] = deque(rgraph.get(target, set()))
    while queue:
        mod = queue.popleft()
        if mod in seen:
            continue
        seen.add(mod)
        queue.extend(p for p in rgraph.get(mod, set()) if p not in seen)
    return seen


def _reachable(graph: dict[str, set[str]], start: str, goal: str) -> bool:
    seen: set[str] = set()
    queue: deque[str] = deque(graph.get(start, set()))
    while queue:
        mod = queue.popleft()
        if mod == goal:
            return True
        if mod in seen:
            continue
        seen.add(mod)
        queue.extend(c for c in graph.get(mod, set()) if c not in seen)
    return False


def _witness_path(
    graph: dict[str, set[str]], start: str, goal: str
) -> list[str] | None:
    """One shortest ``start``..``goal`` node chain (inclusive), or ``None``.

    Children are visited in sorted order so the witness is deterministic.
    Deliberately local (not imported from ``_graph``) per this module's
    independence design; it also runs over the edge-intersection graph the
    consensus arbiter builds from both backends.
    """
    if start == goal:
        return [start]
    prev: dict[str, str | None] = {start: None}
    queue: deque[str] = deque([start])
    while queue:
        node = queue.popleft()
        for child in sorted(graph.get(node, set())):
            if child in prev:
                continue
            prev[child] = node
            if child == goal:
                path = [child]
                parent = prev[child]
                while parent is not None:
                    path.append(parent)
                    parent = prev[parent]
                return path[::-1]
            queue.append(child)
    return None


# ---------------------------------------------------------------------------
# Per-template rederivation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RederivedAnswer:
    """One backend's independent answer for one task.

    ``answer=None`` means the backend could not produce a well-defined
    answer (target missing from its index, ambiguous question); ``detail``
    carries the reason for the divergence report. A ``None`` answer never
    agrees with anything, so the task is quarantined.

    ``files`` is template-shaped: the answer files for ``file_list`` tasks,
    the AST backend's own witness chain (in chain order) for true
    ``transitive_dependency`` tasks, and the defining-file candidates for
    ``return_type_resolution``.
    """

    answer: _ANSWER
    files: tuple[str, ...] = ()
    detail: str | None = None
    # AST-graph witness modules for true transitive_dependency answers.
    witness_modules: tuple[str, ...] = ()


def rederive_answer(
    spec: ComprehensionTaskSpec, index: AstIndex
) -> RederivedAnswer:
    """Re-derive *spec*'s answer from the AST index."""
    if spec.template == "import_chain":
        return _rederive_import_chain(spec, index)
    if spec.template == "dependency_analysis":
        return _rederive_dependency_analysis(spec, index)
    if spec.template == "return_type_resolution":
        return _rederive_return_type(spec, index)
    if spec.template == "transitive_dependency":
        return _rederive_transitive_dependency(spec, index)
    return RederivedAnswer(
        answer=None, detail=f"unknown template {spec.template!r}"
    )


def _rederive_import_chain(
    spec: ComprehensionTaskSpec, index: AstIndex
) -> RederivedAnswer:
    module = spec.target
    if module not in index.module_to_file:
        return RederivedAnswer(
            answer=None, detail=f"module {module!r} not in ast index"
        )
    transitive = _transitive_importers(index.rgraph, module)
    files = sorted(
        index.module_to_file[m] for m in transitive if m in index.module_to_file
    )
    return RederivedAnswer(answer=files, files=tuple(files))


def _rederive_dependency_analysis(
    spec: ComprehensionTaskSpec, index: AstIndex
) -> RederivedAnswer:
    module, _, func = spec.target.rpartition(".")
    if not module or module not in index.module_to_file:
        return RederivedAnswer(
            answer=None, detail=f"module for {spec.target!r} not in ast index"
        )
    callers: list[str] = []
    for importer in index.rgraph.get(module, set()):
        rel = index.module_to_file.get(importer)
        if rel is None:
            continue
        tree = index.trees.get(rel)
        if tree is None:
            return RederivedAnswer(
                answer=None,
                detail=(
                    f"importer {rel} unparseable: "
                    f"{index.parse_failures.get(rel, 'missing tree')}"
                ),
            )
        if func in _called_names(tree):
            callers.append(rel)
    files = sorted(callers)
    return RederivedAnswer(answer=files, files=tuple(files))


def _called_names(node: ast.AST) -> set[str]:
    """Names invoked as calls (``f(...)`` or ``obj.f(...)``) under *node*."""
    names: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Name):
                names.add(n.func.id)
            elif isinstance(n.func, ast.Attribute):
                names.add(n.func.attr)
    return names


def _rederive_return_type(
    spec: ComprehensionTaskSpec, index: AstIndex
) -> RederivedAnswer:
    rel, _, qualname = spec.target.partition("::")
    class_name, _, method_name = qualname.rpartition(".")
    tree = index.trees.get(rel)
    if tree is None:
        return RederivedAnswer(
            answer=None, detail=f"method file {rel!r} not parseable/indexed"
        )

    called: set[str] = set()
    for cls in ast.walk(tree):
        if not (isinstance(cls, ast.ClassDef) and cls.name == class_name):
            continue
        for item in cls.body:
            if (
                isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and item.name == method_name
            ):
                called |= _called_names(item)
    if not called:
        return RederivedAnswer(
            answer=None,
            detail=(
                f"no calls found in {class_name}.{method_name} "
                f"(method missing or empty)"
            ),
        )

    typed = _top_level_typed_functions(index)
    candidates = {
        (other_rel, text)
        for name in called
        for other_rel, text in typed.get(name, ())
        if other_rel != rel
    }
    annotations = {text for _, text in candidates}
    if not annotations:
        return RederivedAnswer(
            answer=None,
            detail="no cross-file typed function call found by ast",
        )
    if len(annotations) > 1:
        return RederivedAnswer(
            answer=None,
            detail=(
                "ambiguous: method calls cross-file typed functions with "
                f"differing annotations {sorted(annotations)}"
            ),
        )
    answer = next(iter(annotations))
    # The files where this backend saw the winning annotation defined —
    # the defining_file consensus check compares against these.
    defining = tuple(sorted(f for f, _ in candidates))
    return RederivedAnswer(answer=answer, files=defining)


def _top_level_typed_functions(
    index: AstIndex,
) -> dict[str, list[tuple[str, str]]]:
    """Map function name -> [(file, annotation-source-text)] for top-level
    annotated functions, exactly as written in the source."""
    out: dict[str, list[tuple[str, str]]] = {}
    for rel, tree in index.trees.items():
        source = index.sources[rel]
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.returns is None:
                continue
            text = ast.get_source_segment(source, node.returns)
            if text is None:
                continue
            out.setdefault(node.name, []).append((rel, text.strip()))
    return out


def _rederive_transitive_dependency(
    spec: ComprehensionTaskSpec, index: AstIndex
) -> RederivedAnswer:
    a, sep, b = spec.target.partition("->")
    if not sep or a not in index.graph or b not in index.module_to_file:
        return RederivedAnswer(
            answer=None,
            detail=f"target modules {spec.target!r} not in ast index",
        )
    witness = _witness_path(index.graph, a, b)
    if witness is None:
        return RederivedAnswer(answer=False)
    files = tuple(index.module_to_file[m] for m in witness)
    return RederivedAnswer(
        answer=True, files=files, witness_modules=tuple(witness)
    )


# ---------------------------------------------------------------------------
# Consensus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskConsensus:
    """Consensus outcome for one task: ship or quarantine, with the record."""

    task_id: str
    agreed: bool
    report: dict[str, Any]
    # None when agreed; a short human-readable divergence reason otherwise.
    reason: str | None


@dataclass(frozen=True)
class _ConsensusWitness:
    """Both-backend witness chain for a true transitive_dependency answer."""

    ok: bool
    files: list[str]
    modules: list[str]
    reason: str | None = None


def verify_comprehension_tasks(
    repo_path: Path,
    tasks: list,
    specs: dict[str, ComprehensionTaskSpec],
) -> dict[str, TaskConsensus]:
    """Re-derive every task's answer via the AST backend and decide consensus.

    Returns one :class:`TaskConsensus` per task id. Agreement is exact
    answer equality; anything else — including a task the AST backend
    cannot answer — is quarantined. On top of answer equality,
    ``transitive_dependency`` true answers must yield an edge-intersection
    witness chain and ``return_type_resolution`` a backend-agreed
    ``defining_file`` (module docstring § template-specific validity).
    """
    repo_path = Path(repo_path)
    index = build_ast_index(repo_path)
    # The intersection witness needs the generator's edges too. Rebuilding
    # the regex index here is deterministic (same walk, same repo state) and
    # keeps the public signature unchanged; built lazily because only
    # transitive_dependency tasks consume it.
    regex_graph: dict[str, set[str]] | None = None
    out: dict[str, TaskConsensus] = {}
    for task in tasks:
        spec = specs.get(task.id)
        if spec is None:
            out[task.id] = TaskConsensus(
                task_id=task.id,
                agreed=False,
                report=_report(task.id, None, None, agreed=False),
                reason="no spec registered for task",
            )
            continue
        rederived = rederive_answer(spec, index)
        agreed = _answers_agree(spec.answer, rederived.answer, spec.answer_type)
        reason = None if agreed else _divergence_reason(spec, rederived)
        witness: _ConsensusWitness | None = None
        if agreed and spec.template == "transitive_dependency":
            if regex_graph is None:
                from codeprobe.mining._graph import _build_index

                regex_graph = _build_index(repo_path).graph
            witness = _transitive_witness(spec, rederived, index, regex_graph)
            if not witness.ok:
                agreed = False
                reason = witness.reason
        elif agreed and spec.template == "return_type_resolution":
            defined = str(spec.metadata.get("defined_in") or "")
            if not defined or defined not in rederived.files:
                agreed = False
                reason = (
                    "backends disagree on the defining file: "
                    f"{GENERATOR_BACKEND}={defined!r}, {AST_BACKEND} "
                    f"candidates={sorted(rederived.files)}"
                )
        out[task.id] = TaskConsensus(
            task_id=task.id,
            agreed=agreed,
            report=_report(
                task.id, spec, rederived, agreed=agreed, witness=witness
            ),
            reason=reason,
        )
    return out


def _transitive_witness(
    spec: ComprehensionTaskSpec,
    rederived: RederivedAnswer,
    index: AstIndex,
    regex_graph: dict[str, set[str]],
) -> _ConsensusWitness:
    """Both-backend witness for an agreed transitive_dependency answer.

    False answers carry no chain (``ok`` with empty witness). True answers
    get one shortest path over the edge-intersection of the two graphs —
    every hop certified by both backends. No intersection path despite
    agreed reachability means the routes are backend-exclusive and the
    oracle chain is not well-defined: not ok, quarantine.
    """
    if rederived.answer is False:
        return _ConsensusWitness(ok=True, files=[], modules=[])
    a, _, b = spec.target.partition("->")
    intersection = {
        mod: regex_graph.get(mod, set()) & edges
        for mod, edges in index.graph.items()
    }
    path = _witness_path(intersection, a, b)
    if path is None:
        return _ConsensusWitness(
            ok=False,
            files=[],
            modules=[],
            reason=(
                "reachability agreed but no witness path is valid in both "
                f"backends' graphs: {GENERATOR_BACKEND} route "
                f"{spec.metadata.get('witness_modules')}, {AST_BACKEND} "
                f"route {list(rederived.witness_modules)}"
            ),
        )
    files = [index.module_to_file[m] for m in path]
    return _ConsensusWitness(ok=True, files=files, modules=path)


def _answers_agree(
    expected: _ANSWER, derived: _ANSWER, answer_type: str
) -> bool:
    """Exact-equality agreement, typed per answer_type."""
    if derived is None:
        return False
    if answer_type == "file_list":
        return isinstance(derived, list) and set(cast("list[str]", expected)) == set(derived)
    if answer_type == "boolean":
        return isinstance(derived, bool) and bool(expected) == derived
    if answer_type == "text":
        return isinstance(derived, str) and str(expected).strip() == derived.strip()
    if answer_type == "count":
        return isinstance(derived, int) and int(cast("str | int", expected)) == int(derived)
    return False


def _divergence_reason(
    spec: ComprehensionTaskSpec, rederived: RederivedAnswer
) -> str:
    if rederived.answer is None:
        return rederived.detail or "ast backend produced no answer"
    if spec.answer_type == "file_list":
        gen_set = set(cast("list[str]", spec.answer))
        ast_set = set(cast("list[str]", rederived.answer))
        return (
            f"file lists differ: {GENERATOR_BACKEND} only "
            f"{sorted(gen_set - ast_set)}, {AST_BACKEND} only "
            f"{sorted(ast_set - gen_set)}"
        )
    return (
        f"answers differ: {GENERATOR_BACKEND}={spec.answer!r} "
        f"{AST_BACKEND}={rederived.answer!r}"
    )


def _report(
    task_id: str,
    spec: ComprehensionTaskSpec | None,
    rederived: RederivedAnswer | None,
    *,
    agreed: bool,
    witness: _ConsensusWitness | None = None,
) -> dict[str, Any]:
    """Build the consensus.v1-shaped divergence report for one task.

    ``decision`` and ``backend_results[].{backend, available, files, error}``
    are the exact fields the aoa-bench loader deserializes;
    ``consensus_files``/``defining_file``/``symbol`` feed the answer-task
    oracle chain; the rest is additive triage context (per-backend answers,
    template, target, ``witness_modules``).
    """
    decision = "shipped" if agreed else "quarantined"
    if spec is None or rederived is None:
        return {
            "schema_version": "consensus.v1",
            "task_id": task_id,
            "decision": decision,
            "participating_backends": [],
            "excluded_backends": [],
            "backend_results": [],
            "pair_metrics": [],
            "consensus_files": [],
        }

    is_transitive = spec.template == "transitive_dependency"
    if spec.answer_type == "file_list":
        gen_files = sorted(cast("list[str]", spec.answer))
    elif is_transitive:
        # The regex backend's own witness chain, in chain order.
        gen_files = list(spec.metadata.get("witness_files") or [])
    else:
        gen_files = []
    ast_files = (
        list(rederived.files) if is_transitive else sorted(rederived.files)
    )
    if is_transitive:
        # Only the both-backend intersection witness ships as the oracle
        # chain — full chain, endpoints included (empty for false answers).
        consensus_files = witness.files if (agreed and witness) else []
    else:
        consensus_files = gen_files if (agreed and gen_files) else []
    report: dict[str, Any] = {
        "schema_version": "consensus.v1",
        "task_id": task_id,
        "template": spec.template,
        "symbol": spec.target,
        "defining_file": str(
            spec.metadata.get("defined_in")
            or spec.metadata.get("target_file")
            or ""
        ),
        "answer_type": spec.answer_type,
        "threshold": 1.0,
        "mode": "exact_answer",
        "decision": decision,
        "participating_backends": [GENERATOR_BACKEND, AST_BACKEND],
        "excluded_backends": [],
        "backend_results": [
            {
                "backend": GENERATOR_BACKEND,
                "available": True,
                "participating": True,
                "participation": "participating",
                "n_files": len(gen_files),
                "files": gen_files,
                "error": None,
                "reason": None,
                "answer": spec.answer,
            },
            {
                "backend": AST_BACKEND,
                "available": True,
                "participating": True,
                "participation": "participating",
                "n_files": len(ast_files),
                "files": ast_files,
                "error": None,
                "reason": None,
                "answer": rederived.answer,
                "detail": rederived.detail,
            },
        ],
        "pair_metrics": [
            {
                "backend_a": GENERATOR_BACKEND,
                "backend_b": AST_BACKEND,
                "exact_match": agreed,
            }
        ],
        "consensus_files": consensus_files,
        "consensus_answer": spec.answer if agreed else None,
    }
    if is_transitive:
        report["witness_modules"] = (
            witness.modules if (agreed and witness) else []
        )
    return report


# ---------------------------------------------------------------------------
# Mine-time commit
# ---------------------------------------------------------------------------


def mine_time_commit(repo_path: Path) -> str | None:
    """HEAD commit of *repo_path* at mine time, or ``None`` outside git.

    Recorded in ``tests/ground_truth.json`` (the ``commit`` field the
    aoa-bench loader reads), NOT in ``metadata.ground_truth_commit`` —
    that field drives executor workspace pinning and the R0 driver's expB
    anchor rewrite, and must stay empty for comprehension tasks.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("mine_time_commit: git rev-parse failed: %s", exc)
        return None
    if result.returncode != 0:
        logger.warning(
            "mine_time_commit: %s is not a git repo (%s) — ground truth "
            "will carry no commit",
            repo_path,
            result.stderr.strip(),
        )
        return None
    commit = result.stdout.strip()
    return commit or None
