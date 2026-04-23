"""Cross-repo SDLC task mining (bead codeprobe-aev).

Extends single-repo PR mining to discover tasks whose ground truth spans
multiple repositories. Task families v1:

1. ``callers``  — PR in primary repo modifies a public function ``F``;
   ground truth = files in secondary repos that import/call ``F``.
2. ``refactor`` — PR renames/moves a public symbol; ground truth = files in
   secondary repos needing updates. (Stub — NotImplementedError.)
3. ``dependency`` — PR bumps a dep in repo A; ground truth = files in repos
   B/C that use the changed API. (Stub — NotImplementedError.)

All families emit ``verification_mode=artifact_eval`` with
``ground_truth.json answer_type=file_list``.

ZFC compliance: this module is pure mechanism — it parses diffs via
regex/git, runs ``rg`` subprocesses, and aggregates file lists. It makes
no semantic judgment about which PRs are "good" tasks; that's delegated to
the caller's quality scoring path (inherited from single-repo mining).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from codeprobe.mining.ast_scan import count_references_in_tree
from codeprobe.mining.extractor import (
    get_changed_files,
    list_merged_prs,
)
from codeprobe.mining.retry import RetryTracker, retry_call
from codeprobe.mining.sources import detect_source
from codeprobe.mining.state import MineState
from codeprobe.models.task import (
    RepoRef,
    Task,
    TaskMetadata,
    TaskVerification,
)

logger = logging.getLogger(__name__)


# Matches Python/TS/Go/Java-ish public function definitions in a diff line.
# Structural only (not semantic judgment): we extract identifiers that
# look like newly-defined or modified public functions/methods.
_PY_DEF_RE = re.compile(r"^\+?\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_JS_FN_RE = re.compile(
    r"^\+?\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("
)
_GO_FN_RE = re.compile(r"^\+?\s*func\s+(?:\([^)]*\)\s*)?([A-Z][A-Za-z0-9_]*)\s*\(")

_SYMBOL_EXTRACTORS = (_PY_DEF_RE, _JS_FN_RE, _GO_FN_RE)


@dataclass(frozen=True)
class FileRef:
    """A reference to a file in some repository."""

    repo: str
    path: str
    line: int = 0


@dataclass(frozen=True)
class Symbol:
    """A resolved symbol — name + optional containing file."""

    name: str
    repo: str = ""
    path: str = ""


@runtime_checkable
class SymbolResolver(Protocol):
    """Abstract symbol resolver used by cross-repo mining.

    Implementations must be deterministic for a given input so test
    fixtures produce stable ground truth.
    """

    def find_references(
        self, symbol: str, repos: list[str]
    ) -> list[FileRef]:  # pragma: no cover - protocol
        ...

    def resolve_symbol_at(
        self, repo: str, path: str, line: int
    ) -> Symbol | None:  # pragma: no cover - protocol
        ...


# ---------------------------------------------------------------------------
# RipgrepResolver — offline, deterministic fallback
# ---------------------------------------------------------------------------


class RipgrepResolver:
    """Find symbol references via ``rg`` subprocess, with a pure-Python fallback.

    Deterministic and offline — suitable as a default when Sourcegraph
    credentials are unavailable and for tests. When the ``rg`` binary is
    unavailable (CI images without ripgrep installed), falls back to a
    mechanical :func:`pathlib.Path.rglob` scan that performs the same
    word-boundary match in Python. The fallback is functionally
    equivalent for text source files, just slower.
    """

    # File suffixes we consider source-like for the Python fallback.
    # Purely structural — no semantic content judgment.
    _TEXT_SUFFIXES = frozenset(
        {
            ".py",
            ".pyi",
            ".js",
            ".jsx",
            ".ts",
            ".tsx",
            ".go",
            ".java",
            ".kt",
            ".rs",
            ".rb",
            ".php",
            ".c",
            ".h",
            ".cc",
            ".cpp",
            ".hpp",
            ".swift",
            ".scala",
            ".sh",
            ".toml",
            ".yaml",
            ".yml",
            ".json",
            ".md",
        }
    )

    def __init__(self, rg_binary: str = "rg") -> None:
        self._rg = rg_binary
        self._rg_available: bool | None = None

    def _check_rg(self) -> bool:
        """Detect whether the configured rg binary exists and is executable."""
        if self._rg_available is not None:
            return self._rg_available
        try:
            result = subprocess.run(
                [self._rg, "--version"],
                capture_output=True,
                text=True,
                check=False,
            )
            self._rg_available = result.returncode == 0
        except (FileNotFoundError, OSError):
            self._rg_available = False
        return self._rg_available

    def find_references(self, symbol: str, repos: list[str]) -> list[FileRef]:
        """Return files matching ``\\b<symbol>\\b`` across *repos*.

        Prefers ``rg --word-regexp --files-with-matches`` when available,
        otherwise falls back to a pure-Python scan.
        """
        if not symbol or not repos:
            return []
        if self._check_rg():
            return self._find_via_rg(symbol, repos)
        logger.info("rg binary unavailable; falling back to Python file scan")
        return self._find_via_python(symbol, repos)

    def _find_via_rg(self, symbol: str, repos: list[str]) -> list[FileRef]:
        refs: list[FileRef] = []
        for repo in repos:
            repo_path = Path(repo)
            try:
                result = subprocess.run(
                    [
                        self._rg,
                        "--word-regexp",
                        "--files-with-matches",
                        "--",
                        symbol,
                        str(repo_path),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except (FileNotFoundError, OSError):
                # Binary disappeared between probe and call — fall back.
                self._rg_available = False
                return self._find_via_python(symbol, repos)
            if result.returncode == 1:  # no matches
                continue
            if result.returncode != 0:
                logger.warning(
                    "rg failed for symbol %r in %s: %s",
                    symbol,
                    repo,
                    result.stderr.strip(),
                )
                continue
            for line in result.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rel = str(Path(line).relative_to(repo_path))
                except ValueError:
                    rel = line
                refs.append(FileRef(repo=repo_path.name, path=rel))
        return refs

    def _find_via_python(self, symbol: str, repos: list[str]) -> list[FileRef]:
        """Word-boundary match using Python — mechanical, no heuristics."""
        pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
        refs: list[FileRef] = []
        for repo in repos:
            repo_path = Path(repo)
            if not repo_path.is_dir():
                continue
            for file_path in repo_path.rglob("*"):
                if not file_path.is_file():
                    continue
                # Skip hidden directories (.git, etc.) — structural filter
                if any(
                    part.startswith(".") and part not in (".", "..")
                    for part in file_path.relative_to(repo_path).parts
                ):
                    continue
                if file_path.suffix not in self._TEXT_SUFFIXES:
                    continue
                try:
                    text = file_path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if pattern.search(text):
                    rel = str(file_path.relative_to(repo_path))
                    refs.append(FileRef(repo=repo_path.name, path=rel))
        return refs

    def resolve_symbol_at(self, repo: str, path: str, line: int) -> Symbol | None:
        """Return the symbol name at the given location, if detectable.

        Mechanical: reads the target file and checks whether the line
        contains a function-definition pattern. Returns ``None`` when no
        structural match is found — callers should fall back or skip.
        """
        file_path = Path(repo) / path
        if not file_path.is_file():
            return None
        try:
            text = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
        lines = text.splitlines()
        if line <= 0 or line > len(lines):
            return None
        content = lines[line - 1]
        for regex in _SYMBOL_EXTRACTORS:
            match = regex.match(content)
            if match:
                return Symbol(name=match.group(1), repo=Path(repo).name, path=path)
        return None


# ---------------------------------------------------------------------------
# mine_tasks_multi
# ---------------------------------------------------------------------------


@dataclass
class MultiRepoMineResult:
    """Result of :func:`mine_tasks_multi`."""

    tasks: list[Task]
    ground_truth_files: dict[str, list[str]] = field(default_factory=dict)


def mine_tasks_multi(
    primary: Path,
    secondaries: tuple[Path, ...],
    *,
    count: int,
    family: Literal["callers", "refactor", "dependency"] = "callers",
    symbol_resolver: SymbolResolver,
    state: MineState | None = None,
    retry_tracker: RetryTracker | None = None,
) -> MultiRepoMineResult:
    """Mine cross-repo tasks from ``primary`` against ``secondaries``.

    Only the ``callers`` family is implemented in v1. Refactor/dependency
    families raise ``NotImplementedError`` with a follow-up message.

    *state* optionally persists per-commit progress; *retry_tracker*
    applies the mine-level retry exhaustion budget (INV1) to the
    symbol-resolver calls which are the primary transient-failure source.
    """
    if family == "refactor":
        raise NotImplementedError(
            "cross-repo refactor family not yet implemented "
            "(see bead codeprobe-aev follow-ups)"
        )
    if family == "dependency":
        raise NotImplementedError(
            "cross-repo dependency family not yet implemented "
            "(see bead codeprobe-aev follow-ups)"
        )
    if family != "callers":
        raise ValueError(f"unknown family: {family!r}")

    return _mine_callers(
        primary=primary,
        secondaries=secondaries,
        count=count,
        symbol_resolver=symbol_resolver,
        state=state,
        retry_tracker=retry_tracker,
    )


def _mine_callers(
    *,
    primary: Path,
    secondaries: tuple[Path, ...],
    count: int,
    symbol_resolver: SymbolResolver,
    state: MineState | None = None,
    retry_tracker: RetryTracker | None = None,
) -> MultiRepoMineResult:
    """Implementation of the ``callers`` family.

    Strategy:
    1. List merged PRs in ``primary`` (falls back to recent commits when no
       merge commits exist — useful for small test fixtures).
    2. For each PR, compute modified symbols from the diff by mechanical
       regex on added/removed lines in changed files.
    3. Call ``symbol_resolver.find_references`` against the secondary repos.
    4. If any cross-repo references are found, construct a Task with
       ``additional_repos`` and ``oracle_type="file_list"``.
    """
    source = detect_source(primary)
    logger.info(
        "mine_tasks_multi: primary=%s secondaries=%s count=%d",
        primary,
        [str(s) for s in secondaries],
        count,
    )

    # Attempt to list merged PRs; fall back to linear commit history for
    # simple fixtures that have no merge commits.
    prs = list_merged_prs(source, primary, limit=max(count * 4, 4))
    commit_shas = [pr.merge_commit for pr in prs]
    if not commit_shas:
        commit_shas = _recent_commits(primary, limit=max(count * 4, 4))

    secondary_paths = [str(s) for s in secondaries]
    secondary_names = [s.name for s in secondaries]

    tasks: list[Task] = []
    gt_map: dict[str, list[str]] = {}
    completed = state.completed_shas() if state is not None else frozenset()

    for sha in commit_shas:
        if sha in completed:
            logger.debug("mine_tasks_multi: skipping %s (already completed)", sha[:8])
            continue
        if state is not None:
            state.record_running(sha)
        try:
            changed = get_changed_files(sha, primary)
            symbols = (
                _extract_modified_symbols(primary, sha, changed) if changed else []
            )

            cross_refs: list[FileRef] = []
            matched_symbol: str = ""
            for sym in symbols:
                if retry_tracker is not None:
                    refs = retry_call(
                        lambda s=sym: symbol_resolver.find_references(
                            s, secondary_paths
                        ),
                        tracker=retry_tracker,
                    )
                else:
                    refs = symbol_resolver.find_references(sym, secondary_paths)
                if refs:
                    cross_refs = refs
                    matched_symbol = sym
                    break
        except Exception as exc:
            if state is not None:
                state.record_interrupted(sha, error=repr(exc))
            raise
        finally:
            # Mechanical: if we didn't trip the interrupted branch, this
            # SHA is done — whether or not we produced a task for it.
            if state is not None and state.status(sha) != "interrupted":
                state.record_completed(sha)

        if not changed or not symbols or not cross_refs:
            continue

        # Build task with additional_repos + file_list ground truth.
        repo_refs = tuple(
            RepoRef(
                name=s.name,
                ground_truth_commit=_head_sha(s),
                local_path=str(s.resolve()),
            )
            for s in secondaries
        )
        # Ground truth file paths formatted as "<repo>/<path>" for clarity.
        gt_files = sorted({f"{r.repo}/{r.path}" for r in cross_refs})

        task_id = f"cross-repo-{sha[:8]}"
        metadata = TaskMetadata(
            name=f"cross-repo callers: {matched_symbol}",
            description=(
                f"PR {sha[:8]} modified public symbol `{matched_symbol}` in "
                f"{primary.name}; update dependent call sites in "
                f"{', '.join(secondary_names)}."
            ),
            category="cross-repo-callers",
            task_type="org_scale_cross_repo",
            org_scale=True,
            ground_truth_commit=sha,
            additional_repos=repo_refs,
        )
        verification = TaskVerification(
            type="oracle",
            command="bash tests/test.sh",
            verification_mode="artifact_eval",
            oracle_type="file_list",
            oracle_answer=tuple(gt_files),
        )
        task = Task(
            id=task_id,
            repo=str(primary),
            metadata=metadata,
            verification=verification,
            verification_modes=("artifact_eval",),
        )
        tasks.append(task)
        gt_map[task.id] = gt_files
        if len(tasks) >= count:
            break

    logger.info(
        "mine_tasks_multi: produced %d cross-repo tasks (family=callers)", len(tasks)
    )
    return MultiRepoMineResult(tasks=tasks, ground_truth_files=gt_map)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _recent_commits(repo: Path, limit: int) -> list[str]:
    """Return most-recent commit SHAs in *repo* (linear history)."""
    try:
        result = subprocess.run(
            ["git", "log", f"--max-count={limit}", "--format=%H"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _head_sha(repo: Path) -> str:
    """Return the HEAD SHA of *repo*, or empty string on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


# ---------------------------------------------------------------------------
# discover_related_repos (R8 — AST-ranked cross-repo relation discovery)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RelatedRepoCandidate:
    """A candidate related repository with its AST-evidenced relevance.

    Fields
    ------
    hint:
        The declared dependency name as it appears in the primary repo's
        manifest files — e.g. ``"react"``, ``"lodash"``,
        ``"github.com/foo/bar"``, or a Python package name.
    ast_hits:
        Total reference count in the primary repo's source tree, summed
        across all source files via :mod:`codeprobe.mining.ast_scan`.
    manifest_sources:
        The manifest files in which this dependency was declared
        (e.g. ``("package.json",)``). Sorted, de-duplicated.
    """

    hint: str
    ast_hits: int
    manifest_sources: tuple[str, ...] = ()


def discover_related_repos(
    primary_repo_path: Path,
    hints: Mapping[str, Path] | None = None,
    *,
    interactive: bool | None = None,
    cross_repo_confirmed: bool = False,
) -> list[RelatedRepoCandidate]:
    """Discover repos related to *primary_repo_path* via AST evidence.

    Walks supported manifests in the primary repo, builds a candidate set
    of declared dependencies, scans the primary repo's source tree for AST
    references to each candidate, and returns the hit-ranked list.

    Critical invariant: candidates with a manifest declaration but **zero**
    AST references are REJECTED (absent from the returned list). This
    prevents stale/shadowed manifest entries from contaminating cross-repo
    mining.

    Parameters
    ----------
    primary_repo_path:
        Path to the primary repo root. Must contain at least one manifest
        (``go.mod``, ``package.json``, or ``pyproject.toml``) for any
        candidates to be produced.
    hints:
        Optional mapping of declared-dependency-name → source-tree path.
        Unused at scan time (we scan the *primary* for refs to the
        candidate name), but retained for future extension and advisory
        inclusion in the returned records.
    interactive:
        ``None`` (default) auto-detects via :func:`sys.stdin.isatty`.
        ``True`` forces an interactive ``y/n`` confirmation prompt per
        candidate. ``False`` disables prompting.
    cross_repo_confirmed:
        Required to be ``True`` in non-interactive mode (caller passed
        ``--cross-repo`` or equivalent). Guards against silent cross-repo
        runs in CI or scripted contexts.

    Returns
    -------
    list[RelatedRepoCandidate]
        Ordered by ``ast_hits`` descending, ties broken by hint name
        ascending. Output is **advisory** — callers must still pass the
        accepted candidates through ``mine_tasks_multi``.

    Raises
    ------
    RuntimeError
        If ``interactive`` is ``False`` (or auto-detected as such) and
        ``cross_repo_confirmed`` is ``False``.
    """
    primary = Path(primary_repo_path).resolve()
    manifest_deps = _parse_manifests(primary)

    if interactive is None:
        interactive = _stdin_isatty()

    if not interactive and not cross_repo_confirmed:
        raise RuntimeError(
            "discover_related_repos: non-interactive mode requires explicit "
            "cross_repo_confirmed=True (pass --cross-repo on the CLI)."
        )

    if not manifest_deps:
        return []

    _ = hints  # currently advisory-only; kept in signature per R8 spec

    # Rank candidates by AST hit count in the primary source tree.
    ranked: list[RelatedRepoCandidate] = []
    for dep_name, manifest_sources in manifest_deps.items():
        hits = count_references_in_tree(primary, [dep_name])
        if hits <= 0:
            # REJECT manifest-only declarations with zero AST evidence.
            continue
        ranked.append(
            RelatedRepoCandidate(
                hint=dep_name,
                ast_hits=hits,
                manifest_sources=tuple(sorted(set(manifest_sources))),
            )
        )

    ranked.sort(key=lambda c: (-c.ast_hits, c.hint))

    if interactive:
        ranked = _interactive_confirm(ranked)

    return ranked


def _stdin_isatty() -> bool:
    """Wrapper so tests can monkeypatch stdin TTY detection easily."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _interactive_confirm(
    candidates: list[RelatedRepoCandidate],
) -> list[RelatedRepoCandidate]:
    """Prompt the user to confirm each candidate (``y`` accept / ``n`` reject)."""
    accepted: list[RelatedRepoCandidate] = []
    for cand in candidates:
        prompt = (
            f"Include related repo hint '{cand.hint}' "
            f"(ast_hits={cand.ast_hits}, from={','.join(cand.manifest_sources)})? [y/N]: "
        )
        try:
            reply = input(prompt)
        except EOFError:
            reply = ""
        if reply.strip().lower() in ("y", "yes"):
            accepted.append(cand)
    return accepted


# ---------------------------------------------------------------------------
# Manifest parsers
# ---------------------------------------------------------------------------


def _parse_manifests(primary: Path) -> dict[str, list[str]]:
    """Parse all supported manifests; return ``{dep_name: [manifest_source, ...]}``."""
    deps: dict[str, list[str]] = {}

    go_mod = primary / "go.mod"
    if go_mod.is_file():
        for name in _parse_go_mod(go_mod):
            deps.setdefault(name, []).append("go.mod")

    package_json = primary / "package.json"
    if package_json.is_file():
        for name in _parse_package_json(package_json):
            deps.setdefault(name, []).append("package.json")

    pyproject = primary / "pyproject.toml"
    if pyproject.is_file():
        for name in _parse_pyproject(pyproject):
            deps.setdefault(name, []).append("pyproject.toml")

    return deps


def _parse_go_mod(path: Path) -> set[str]:
    """Parse a ``go.mod`` file and return declared module paths.

    Line-by-line parser — handles both single-line ``require foo v1.2.3``
    and block ``require (...)`` forms. Ignores comments, the ``module``
    declaration itself, and ``replace`` / ``exclude`` directives.
    """
    names: set[str] = set()
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return names
    in_block = False
    for raw in text.splitlines():
        line = raw.split("//", 1)[0].strip()
        if not line:
            continue
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block:
            if line == ")":
                in_block = False
                continue
            mod = _split_first_token(line)
            if mod:
                names.add(mod)
            continue
        if line.startswith("require "):
            rest = line[len("require ") :].strip()
            if rest.startswith("("):  # edge-case: ``require (`` with trailing
                in_block = True
                continue
            mod = _split_first_token(rest)
            if mod:
                names.add(mod)
    return names


def _split_first_token(line: str) -> str | None:
    parts = line.split()
    if not parts:
        return None
    tok = parts[0].strip().strip('"')
    if not tok or tok in (")",):
        return None
    return tok


def _parse_package_json(path: Path) -> set[str]:
    """Parse ``package.json`` dependency fields (npm / yarn / pnpm share this).

    Reads ``dependencies``, ``devDependencies``, ``peerDependencies``, and
    ``optionalDependencies`` — the same shape is used by npm, yarn, and
    pnpm.
    """
    names: set[str] = set()
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except (OSError, json.JSONDecodeError):
        return names
    if not isinstance(data, dict):
        return names
    for key in (
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
    ):
        block = data.get(key)
        if isinstance(block, dict):
            for dep_name in block:
                if isinstance(dep_name, str) and dep_name:
                    names.add(dep_name)
    return names


def _parse_pyproject(path: Path) -> set[str]:
    """Parse ``pyproject.toml`` — both PEP 621 and Poetry layouts.

    Reads ``project.dependencies``, ``project.optional-dependencies.*``,
    ``tool.poetry.dependencies``, and ``tool.poetry.dev-dependencies``.
    Strips version specifiers / markers to produce canonical package names.
    """
    names: set[str] = set()
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return names

    project = data.get("project")
    if isinstance(project, dict):
        deps = project.get("dependencies")
        if isinstance(deps, list):
            for item in deps:
                normalized = _normalize_python_dep(item)
                if normalized:
                    names.add(normalized)
        optional = project.get("optional-dependencies")
        if isinstance(optional, dict):
            for extra in optional.values():
                if isinstance(extra, list):
                    for item in extra:
                        normalized = _normalize_python_dep(item)
                        if normalized:
                            names.add(normalized)

    poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data.get("tool"), dict) else {}
    if isinstance(poetry, dict):
        for key in ("dependencies", "dev-dependencies"):
            block = poetry.get(key)
            if isinstance(block, dict):
                for dep_name in block:
                    if isinstance(dep_name, str) and dep_name.lower() != "python":
                        names.add(dep_name)

    return names


_PEP508_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)")


def _normalize_python_dep(raw: object) -> str | None:
    """Strip version specifiers from a PEP 508 dep string; return the name only."""
    if not isinstance(raw, str):
        return None
    match = _PEP508_NAME_RE.match(raw)
    if not match:
        return None
    return match.group(1)


# ---------------------------------------------------------------------------
# Legacy helper kept for compatibility with existing _mine_callers path
# ---------------------------------------------------------------------------


def _extract_modified_symbols(
    repo: Path, sha: str, changed_files: list[str]
) -> list[str]:
    """Extract public function names modified in *sha* within *changed_files*.

    Purely structural: runs ``git show <sha> -- <file>`` and regex-matches
    definition lines. No semantic judgment.
    """
    symbols: list[str] = []
    seen: set[str] = set()
    for path in changed_files:
        try:
            result = subprocess.run(
                ["git", "show", f"{sha}", "--", path],
                cwd=repo,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return []
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
                        continue  # skip private by convention
                    if name not in seen:
                        seen.add(name)
                        symbols.append(name)
                    break
    return symbols
