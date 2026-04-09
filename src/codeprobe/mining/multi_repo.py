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

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from codeprobe.mining.extractor import (
    _get_changed_files,
    list_merged_prs,
)
from codeprobe.mining.sources import detect_source
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
) -> MultiRepoMineResult:
    """Mine cross-repo tasks from ``primary`` against ``secondaries``.

    Only the ``callers`` family is implemented in v1. Refactor/dependency
    families raise ``NotImplementedError`` with a follow-up message.
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
    )


def _mine_callers(
    *,
    primary: Path,
    secondaries: tuple[Path, ...],
    count: int,
    symbol_resolver: SymbolResolver,
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

    for sha in commit_shas:
        changed = _get_changed_files(sha, primary)
        if not changed:
            continue
        symbols = _extract_modified_symbols(primary, sha, changed)
        if not symbols:
            continue

        cross_refs: list[FileRef] = []
        matched_symbol: str = ""
        for sym in symbols:
            refs = symbol_resolver.find_references(sym, secondary_paths)
            if refs:
                cross_refs = refs
                matched_symbol = sym
                break
        if not cross_refs:
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
