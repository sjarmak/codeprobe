"""AST-based symbol resolver — tool-independent ground truth.

A third backend alongside :class:`RipgrepResolver` (mechanical text match)
and :class:`SourcegraphSymbolResolver` (cross-repo code intelligence).

Why this exists
---------------

``--mcp-families`` mining historically suffered from a tautology: ground
truth came from the same code-intelligence tool the agent-under-eval was
using through MCP. AstResolver is the foundation for ending that
tautology — it produces ground truth from a code-intel-tool-INDEPENDENT
source: a real language parser run locally.

Scope (v1)
----------

In scope:

- **Python**: real :mod:`ast` walk. Resolves direct calls (``Symbol()``),
  method calls (``obj.Symbol()``) where ``obj`` is not an imported
  module, and qualified imports (``from m import Symbol`` then call).
- **Go**: real ``go/parser`` walk via an embedded Go helper invoked with
  ``go run``. Resolves method declarations, method calls
  (``recv.Symbol(...)``) where ``recv`` is not an imported package
  alias, and bare function calls.

Out of scope (deferred to v2):

- Cross-package Go type inference. ``a.Symbol()`` where ``a`` is the
  return value of an imported package's constructor is NOT resolved
  through type inference; it's resolved structurally (the file is
  included if ``a.Symbol(...)`` call exists and ``a`` is not a package
  alias).
- Macro-heavy languages (Rust, C++).
- Dynamic dispatch beyond Go interfaces and Python duck typing.
- Files with parse errors are skipped, not failed.

Compared to other backends
--------------------------

- vs. :class:`RipgrepResolver`: rejects matches inside comments,
  strings, and unrelated-package selectors. Higher precision.
- vs. :class:`SourcegraphSymbolResolver`: no network calls, no auth,
  no cross-package type inference. Lower recall on dispatched-method
  patterns. Higher availability and fully offline.

ZFC compliance
--------------

Pure mechanism: parse + walk + filter. No semantic judgments, no
hardcoded thresholds. Structural filters (file extensions, hidden
directories, import-set membership) are allowed per ZFC §Allowed.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from codeprobe.mining.multi_repo import FileRef, Symbol

logger = logging.getLogger(__name__)


# Default subprocess timeout for the Go scanner. The bead's perf bound is
# <30s for a 1000-file Go repo; 120s gives generous headroom for very
# large repos before we conclude the toolchain is unhealthy.
_GO_SCAN_TIMEOUT_SECONDS = 120

# Path to the embedded Go AST scanner program (shipped inside the
# package; run with ``go run`` so we don't ship a compiled binary).
_GO_SCANNER_PATH = (
    Path(__file__).parent / "_go_ast_scanner" / "scanner.go"
).resolve()


@dataclass(frozen=True)
class _GoRef:
    path: str
    line: int
    kind: str


class AstResolver:
    """SymbolResolver implementation backed by real language parsers.

    Implements the ``SymbolResolver`` Protocol from
    :mod:`codeprobe.mining.multi_repo` via duck typing — it does not
    import the Protocol class to avoid a circular dependency.

    Supports:

    - Python: stdlib :mod:`ast`
    - Go: ``go/parser`` invoked via ``go run`` of an embedded helper

    Files in unsupported languages are skipped silently.
    """

    def __init__(
        self,
        defining_file: str = "",
        *,
        go_binary: str = "go",
        max_workers: int = 4,
        scope: str = "auto",
    ) -> None:
        """Construct an AstResolver.

        Parameters
        ----------
        defining_file:
            Repo-relative path where the symbol is defined. When set,
            the resolver restricts results to the symbol's package
            (intra-package scoping) — matches SG's typed
            ``find_references`` semantics without needing full Go type
            inference. Pass ``""`` to scan the entire repo.
        scope:
            ``"auto"`` (default) → ``"package"`` when *defining_file* is
            set, else ``"repo"``. ``"package"`` forces same-package
            scoping; ``"repo"`` disables it. Same-package = same
            directory as *defining_file*.
        """
        self._defining_file = defining_file
        self._go_binary = go_binary
        self._max_workers = max(1, int(max_workers))
        self._go_available: bool | None = None
        scope_norm = (scope or "auto").lower()
        if scope_norm not in ("auto", "package", "repo"):
            raise ValueError(
                f"AstResolver: invalid scope {scope!r} "
                "(expected 'auto', 'package', or 'repo')"
            )
        self._scope = scope_norm

    # ------------------------------------------------------------------
    # SymbolResolver Protocol
    # ------------------------------------------------------------------

    def find_references(self, symbol: str, repos: list[str]) -> list[FileRef]:
        """Return references to *symbol* across *repos*.

        Each repo path must point to a directory on disk; URL inputs are
        not supported by this backend.
        """
        from codeprobe.mining.multi_repo import FileRef

        if not symbol or not repos:
            return []

        refs: list[FileRef] = []
        for repo in repos:
            repo_path = Path(repo)
            if not repo_path.is_dir():
                logger.info("AstResolver: skipping non-directory %s", repo)
                continue
            repo_name = repo_path.name
            for rel_path in self._scan_repo(repo_path, symbol):
                refs.append(FileRef(repo=repo_name, path=rel_path))
        return refs

    def resolve_symbol_at(
        self, repo: str, path: str, line: int
    ) -> Symbol | None:
        """Return the :class:`Symbol` defined at *line* in *repo/path*.

        For Python files, parses the file via :mod:`ast` and returns the
        innermost ``FunctionDef``/``AsyncFunctionDef``/``ClassDef`` whose
        ``lineno`` equals *line*. For Go files, falls back to a
        structural pattern match — a real Go AST roundtrip per call would
        be too expensive for the typical use case.
        """
        from codeprobe.mining.multi_repo import Symbol

        file_path = Path(repo) / path
        if not file_path.is_file():
            return None

        suffix = file_path.suffix.lower()
        if suffix in (".py", ".pyi"):
            return _resolve_python_symbol_at(file_path, line, repo, path)
        if suffix == ".go":
            return _resolve_go_symbol_at(file_path, line, repo, path, Symbol)
        return None

    # ------------------------------------------------------------------
    # Internal: per-repo dispatch
    # ------------------------------------------------------------------

    def _scan_repo(self, repo_path: Path, symbol: str) -> set[str]:
        """Return the set of repo-relative paths in *repo_path* that
        reference *symbol*."""
        files: set[str] = set()
        files.update(self._scan_python_repo(repo_path, symbol))
        files.update(self._scan_go_repo(repo_path, symbol))
        return self._apply_scope(files)

    def _apply_scope(self, files: set[str]) -> set[str]:
        """Restrict *files* to the symbol's package when scoping is on.

        Same-package = same directory as ``self._defining_file``. This
        is the lightweight stand-in for full Go type inference: SG's
        typed ``find_references`` returns intra-package callers
        plus cross-package callers that explicitly use the receiver
        type. v1 covers the intra-package case mechanically; cross-
        package type inference is deferred (see module docstring).
        """
        scope = self._scope
        if scope == "auto":
            scope = "package" if self._defining_file else "repo"
        if scope == "repo":
            return files
        if not self._defining_file:
            return files
        # Use the defining file's directory as the package boundary.
        # Path normalisation matches the scanner's repo-relative output.
        defining = Path(self._defining_file)
        package_dir = str(defining.parent).replace("\\", "/")
        if package_dir in (".", ""):
            return {f for f in files if "/" not in f}
        prefix = package_dir.rstrip("/") + "/"
        return {f for f in files if f == self._defining_file or f.startswith(prefix)}

    # ------------------------------------------------------------------
    # Python scanning
    # ------------------------------------------------------------------

    def _scan_python_repo(self, repo_path: Path, symbol: str) -> set[str]:
        py_files = list(_iter_source_files(repo_path, (".py", ".pyi")))
        if not py_files:
            return set()

        out: set[str] = set()

        def _scan(path: Path) -> str | None:
            try:
                if _python_file_references(path, symbol):
                    return str(path.relative_to(repo_path))
            except Exception as exc:  # pragma: no cover - defensive
                logger.debug("AstResolver: python scan error %s: %s", path, exc)
            return None

        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            for fut in as_completed(pool.submit(_scan, p) for p in py_files):
                rel = fut.result()
                if rel:
                    out.add(rel)
        return out

    # ------------------------------------------------------------------
    # Go scanning
    # ------------------------------------------------------------------

    def _scan_go_repo(self, repo_path: Path, symbol: str) -> set[str]:
        if not self._check_go_binary():
            return set()

        try:
            result = subprocess.run(
                [
                    self._go_binary,
                    "run",
                    str(_GO_SCANNER_PATH),
                    "-repo",
                    str(repo_path),
                    "-symbol",
                    symbol,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=_GO_SCAN_TIMEOUT_SECONDS,
                env=_safe_go_env(),
            )
        except subprocess.TimeoutExpired:
            logger.warning(
                "AstResolver: Go scan timed out after %ds for %s "
                "(symbol=%s)",
                _GO_SCAN_TIMEOUT_SECONDS,
                repo_path,
                symbol,
            )
            return set()
        except (FileNotFoundError, OSError) as exc:
            logger.warning(
                "AstResolver: Go scanner invocation failed (%s); skipping",
                exc,
            )
            return set()

        if result.returncode != 0:
            logger.warning(
                "AstResolver: Go scanner exit=%d for %s: %s",
                result.returncode,
                repo_path,
                result.stderr.strip(),
            )
            return set()

        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            logger.warning(
                "AstResolver: Go scanner produced invalid JSON: %s", exc
            )
            return set()

        files = payload.get("files") or []
        return {f for f in files if isinstance(f, str)}

    def _check_go_binary(self) -> bool:
        if self._go_available is not None:
            return self._go_available
        path = shutil.which(self._go_binary)
        self._go_available = path is not None
        if not self._go_available:
            logger.info(
                "AstResolver: %r not on PATH; Go files will be skipped",
                self._go_binary,
            )
        return self._go_available


# ----------------------------------------------------------------------
# Python helpers
# ----------------------------------------------------------------------


def _iter_source_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    """Return source files under *root* matching any of *suffixes*.

    Skips hidden directories (``.git``, ``.venv``, ``node_modules``,
    ``vendor``) — purely structural filtering.
    """
    skipped_names = frozenset({".git", ".venv", "node_modules", "vendor"})
    out: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel_parts = path.relative_to(root).parts
        except ValueError:
            continue
        if any(p in skipped_names for p in rel_parts):
            continue
        if any(p.startswith(".") and p not in (".", "..") for p in rel_parts):
            continue
        if path.suffix.lower() in suffixes:
            out.append(path)
    return out


def _python_file_references(path: Path, symbol: str) -> bool:
    """Return True if *path* contains an AST reference to *symbol*.

    A reference is any of:

    - ``Symbol(...)`` direct call (Name node at call position)
    - ``obj.Symbol(...)`` method call where ``obj`` is NOT an imported
      module alias known to this file
    - ``from m import Symbol`` or ``import Symbol`` (qualifies a usage
      site even when the call is dynamic)
    - ``def Symbol(...)`` / ``async def Symbol(...)`` definition
    - ``class Symbol(...)`` definition
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False

    imports = _collect_python_import_aliases(tree)

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == symbol:
                return True
        elif isinstance(node, ast.Name):
            if node.id == symbol:
                return True
        elif isinstance(node, ast.Attribute):
            if node.attr != symbol:
                continue
            # Skip <imported_module>.Symbol — that's a qualified
            # reference to an import, not a local method-call target.
            if isinstance(node.value, ast.Name) and node.value.id in imports:
                continue
            return True
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == symbol or alias.asname == symbol:
                    return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                # An ``import x.y.z`` brings ``x`` into scope; only count
                # the leaf if the user explicitly aliased it.
                if alias.asname == symbol or alias.name == symbol:
                    return True
    return False


def _collect_python_import_aliases(tree: ast.AST) -> frozenset[str]:
    """Return the set of names a Python module uses to refer to imports.

    Used so ``foo.Symbol(...)`` where ``foo`` is an imported module is
    NOT treated as a method call on a local object.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    aliases.add(alias.asname)
                else:
                    # ``import a.b.c`` brings ``a`` into scope.
                    aliases.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            # ``from a import b`` brings ``b`` into scope; we already
            # match this kind of import via ImportFrom node inspection
            # in _python_file_references. Nothing to add here.
            pass
    return frozenset(aliases)


def _resolve_python_symbol_at(
    path: Path, line: int, repo: str, rel_path: str
) -> Symbol | None:
    from codeprobe.mining.multi_repo import Symbol

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None

    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ) and getattr(node, "lineno", -1) == line:
            return Symbol(name=node.name, repo=Path(repo).name, path=rel_path)
    return None


# ----------------------------------------------------------------------
# Go helpers
# ----------------------------------------------------------------------


def _resolve_go_symbol_at(
    path: Path, line: int, repo: str, rel_path: str, symbol_cls: type
):
    """Identify the Go func/method declared at *line* in *path*.

    Mechanical: read the line, regex-match a func/method declaration
    pattern, return :class:`Symbol`. Avoids spinning up a Go subprocess
    on every call.
    """
    import re

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    if line <= 0 or line > len(lines):
        return None
    content = lines[line - 1]
    func_re = re.compile(
        r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_][A-Za-z0-9_]*)\s*\("
    )
    match = func_re.match(content)
    if not match:
        return None
    return symbol_cls(name=match.group(1), repo=Path(repo).name, path=rel_path)


def _safe_go_env() -> dict[str, str]:
    """Return an environment suitable for ``go run`` invocations.

    Forces ``GO111MODULE=off`` so the scanner's single-file ``go run``
    works inside repos that have their own ``go.mod`` constraints (it
    has no external dependencies and is a single .go file).
    """
    env = dict(os.environ)
    env["GO111MODULE"] = "off"
    # Use a stable, sandboxed cache so we don't fight a user's pinned
    # GOCACHE/GOMODCACHE. Falls back to the default when unset.
    env.setdefault("GOFLAGS", "")
    return env
