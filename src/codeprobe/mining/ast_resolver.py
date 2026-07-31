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
  ``go run``. Resolves method declarations, local receiver calls,
  bare function calls, and package-qualified calls whose import path
  matches the package containing the defining file.

Out of scope (deferred to v2):

- Cross-package Go receiver-type inference. ``a.Symbol()`` where ``a`` is the
  return value of an imported package's constructor is not included by
  target-aware ``auto`` scope. Explicit ``repo`` scope can match it
  structurally, without proving the receiver type.
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
import re
import shutil
import stat
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
_MAX_SOURCE_FILE_BYTES = 8 * 1024 * 1024

# Path to the embedded Go AST scanner program (shipped inside the
# package; run with ``go run`` so we don't ship a compiled binary).
_GO_SCANNER_PATH = (
    Path(__file__).parent / "_go_ast_scanner" / "scanner.go"
).resolve()
_GO_VERSION_NUMBER = r"[0-9]{1,3}"
_GO_DEVEL_REVISION = r"[0-9a-f]{6,64}"
_GO_DEVEL_TIMESTAMP = (
    r"(?: [A-Z][a-z]{2} [A-Z][a-z]{2} "
    r"(?:[12][0-9]|3[01]| [1-9]) [0-9]{2}:[0-9]{2}:[0-9]{2} "
    r"[0-9]{4} [+-][0-9]{4})?"
)
_GO_VERSION_PATTERN = re.compile(
    r"^(?:"
    rf"go(?P<release_major>{_GO_VERSION_NUMBER})\."
    rf"(?P<release_minor>{_GO_VERSION_NUMBER})"
    r"(?:"
    rf"(?:\.(?P<patch>{_GO_VERSION_NUMBER})"
    r"(?:-[A-Za-z0-9][A-Za-z0-9.+_-]*)?)"
    rf"|(?P<prerelease>(?:beta|rc){_GO_VERSION_NUMBER})"
    r")?"
    rf"|devel go(?P<devel_major>{_GO_VERSION_NUMBER})\."
    rf"(?P<devel_minor>{_GO_VERSION_NUMBER})-{_GO_DEVEL_REVISION}"
    rf"{_GO_DEVEL_TIMESTAMP}"
    rf"|go(?P<current_devel_major>{_GO_VERSION_NUMBER})\."
    rf"(?P<current_devel_minor>{_GO_VERSION_NUMBER})"
    rf"-devel_{_GO_DEVEL_REVISION}{_GO_DEVEL_TIMESTAMP}"
    r")$"
)
_GO_ENV_KEYS = (
    "GOCACHE",
    "GOPATH",
    "GOROOT",
    "GOTMPDIR",
    "HOME",
    "PATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "XDG_CACHE_HOME",
)


def go_toolchain_status(go_binary: str = "go") -> tuple[bool, str]:
    """Return whether *go_binary* can run the contained Go AST scanner."""
    path = shutil.which(go_binary)
    if path is None:
        return False, "Go executable not found; Go 1.24 or newer is required"
    try:
        result = subprocess.run(
            [path, "env", "GOVERSION"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
            env=_safe_go_env(),
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False, (
            "unable to determine Go version; Go 1.24 or newer is required"
        )

    if result.returncode != 0:
        return False, (
            "unable to determine Go version; Go 1.24 or newer is required"
        )
    reported = result.stdout.strip()
    version = _GO_VERSION_PATTERN.fullmatch(reported)
    if version is None:
        return False, (
            "unrecognized Go version; Go 1.24 or newer is required"
        )
    release_major = version.group("release_major")
    release_minor = version.group("release_minor")
    major = (
        release_major
        or version.group("devel_major")
        or version.group("current_devel_major")
    )
    minor = (
        release_minor
        or version.group("devel_minor")
        or version.group("current_devel_minor")
    )
    if major is None or minor is None:  # pragma: no cover - regex invariant
        return False, (
            "unrecognized Go version; Go 1.24 or newer is required"
        )
    supported = (
        int(major),
        int(minor),
    ) >= (1, 24)
    if release_major is not None and release_minor is not None:
        displayed = f"go{release_major}.{release_minor}"
        patch = version.group("patch")
        prerelease = version.group("prerelease")
        if patch is not None:
            displayed += f".{patch}"
        elif prerelease is not None:
            displayed += prerelease
    else:
        displayed = f"go{major}.{minor} development"
    if not supported:
        return False, f"{displayed} found; Go 1.24 or newer is required"
    return True, f"{displayed} (supported)"


@dataclass(frozen=True)
class _GoTarget:
    import_path: str = ""
    package_name: str = ""
    resolved: bool = False


@dataclass(frozen=True)
class _GoScanResult:
    files: frozenset[str]
    target: _GoTarget


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
            Python results are restricted to the symbol's package. Go
            results also retain callers that import that exact package
            and invoke ``pkg.Symbol``. Pass ``""`` for repo-wide
            structural scanning without a package target.
        scope:
            ``"auto"`` (default) applies package scope to Python and
            target-aware import scope to Go when *defining_file* is set.
            ``"package"`` forces same-directory results; ``"repo"``
            enables repo-wide structural matches.
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

        if self._defining_file:
            primary_repo = Path(repos[0])
            if (
                not primary_repo.is_dir()
                or _read_repo_text(primary_repo, self._defining_file) is None
            ):
                logger.warning(
                    "AstResolver: defining file %r is not a readable, bounded "
                    "regular file in primary repository %s; refusing to emit "
                    "unscoped evidence",
                    self._defining_file,
                    primary_repo,
                )
                return []

        refs: list[FileRef] = []
        go_target = _GoTarget()
        for index, repo in enumerate(repos):
            repo_path = Path(repo)
            if not repo_path.is_dir():
                logger.info("AstResolver: skipping non-directory %s", repo)
                continue
            repo_name = repo_path.name
            python_files = self._apply_scope(
                self._scan_python_repo(repo_path, symbol)
            )
            if index > 0 and self._defining_file and not go_target.resolved:
                go_scan = _GoScanResult(frozenset(), go_target)
            else:
                go_scan = self._scan_go_repo(
                    repo_path,
                    symbol,
                    defining_file=self._defining_file if index == 0 else "",
                    target=go_target,
                )
            if index == 0:
                go_target = go_scan.target
            for rel_path in python_files | go_scan.files:
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

        text = _read_repo_text(Path(repo), path)
        if text is None:
            return None

        suffix = Path(path).suffix.lower()
        if suffix in (".py", ".pyi"):
            return _resolve_python_symbol_at(text, line, repo, path)
        if suffix == ".go":
            return _resolve_go_symbol_at(text, line, repo, path, Symbol)
        return None

    def _apply_scope(self, files: set[str]) -> set[str]:
        """Restrict Python *files* to the symbol's package when scoping is on.

        The Go helper applies scope itself so it can retain exact
        package-qualified references outside the defining directory.
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
                rel = str(path.relative_to(repo_path))
                text = _read_repo_text(repo_path, rel)
                if text is not None and _python_text_references(text, symbol):
                    return rel
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

    def _scan_go_repo(
        self,
        repo_path: Path,
        symbol: str,
        *,
        defining_file: str,
        target: _GoTarget,
    ) -> _GoScanResult:
        if not self._check_go_binary():
            return _GoScanResult(frozenset(), target)

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
                    "-defining-file",
                    defining_file,
                    "-target-import-path",
                    target.import_path,
                    "-target-package-name",
                    target.package_name,
                    f"-target-resolved={str(target.resolved).lower()}",
                    "-scope",
                    self._scope,
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
            return _GoScanResult(frozenset(), target)
        except (FileNotFoundError, OSError) as exc:
            logger.warning(
                "AstResolver: Go scanner invocation failed (%s); skipping",
                exc,
            )
            return _GoScanResult(frozenset(), target)

        if result.returncode != 0:
            logger.warning(
                "AstResolver: Go scanner exit=%d for %s: %s",
                result.returncode,
                repo_path,
                result.stderr.strip(),
            )
            return _GoScanResult(frozenset(), target)

        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            logger.warning(
                "AstResolver: Go scanner produced invalid JSON: %s", exc
            )
            return _GoScanResult(frozenset(), target)

        files = payload.get("files") or []
        discovered_target = _GoTarget(
            import_path=str(payload.get("target_import_path") or ""),
            package_name=str(payload.get("target_package_name") or ""),
            resolved=payload.get("target_resolved") is True,
        )
        return _GoScanResult(
            frozenset(f for f in files if isinstance(f, str)),
            target if target.resolved else discovered_target,
        )

    def _check_go_binary(self) -> bool:
        if self._go_available is not None:
            return self._go_available
        supported, detail = go_toolchain_status(self._go_binary)
        if not supported:
            logger.warning(
                "AstResolver: %s; Go files will be skipped",
                detail,
            )
        self._go_available = supported
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
        if path.is_symlink() or not path.is_file():
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


def _read_repo_text(root: Path, relative_path: str) -> str | None:
    """Read one bounded regular file without following path symlinks."""
    candidate = Path(relative_path)
    parts = candidate.parts
    if candidate.is_absolute() or not parts or any(p in ("", ".", "..") for p in parts):
        return None
    try:
        root = root.resolve(strict=True)
    except OSError:
        return None

    nofollow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    supports_dir_fd: set[object] | None = getattr(os, "supports_dir_fd", None)
    if (
        nofollow is None
        or directory is None
        or supports_dir_fd is None
        or os.open not in supports_dir_fd
    ):
        return None

    directory_flags = os.O_RDONLY | directory | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= nofollow
    file_flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    file_flags |= nofollow
    descriptors: list[int] = []
    try:
        directory_fd = os.open(root, directory_flags)
        descriptors.append(directory_fd)
        for component in parts[:-1]:
            directory_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            descriptors.append(directory_fd)
        file_fd = os.open(parts[-1], file_flags, dir_fd=directory_fd)
        descriptors.append(file_fd)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            return None
        with os.fdopen(file_fd, "rb", closefd=False) as stream:
            data = stream.read(_MAX_SOURCE_FILE_BYTES + 1)
        if len(data) > _MAX_SOURCE_FILE_BYTES:
            return None
        return data.decode("utf-8", errors="replace")
    except OSError:
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _python_text_references(text: str, symbol: str) -> bool:
    """Return True if *text* contains an AST reference to *symbol*.

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
    text: str, line: int, repo: str, rel_path: str
) -> Symbol | None:
    from codeprobe.mining.multi_repo import Symbol

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
    text: str, line: int, repo: str, rel_path: str, symbol_cls: type[Symbol]
) -> Symbol | None:
    """Identify the Go func/method declared at *line* in *text*.

    Mechanical: read the line, regex-match a func/method declaration
    pattern, return :class:`Symbol`. Avoids spinning up a Go subprocess
    on every call.
    """
    import re

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
    env = {
        key: os.environ[key]
        for key in _GO_ENV_KEYS
        if os.environ.get(key)
    }
    env.update(
        {
            "CGO_ENABLED": "0",
            "GO111MODULE": "off",
            "GOENV": "off",
            "GOFLAGS": "",
            "GOTOOLCHAIN": "local",
        }
    )
    return env
