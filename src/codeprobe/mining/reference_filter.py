"""Filter org-scale ground-truth candidates to files that plausibly reference
the defining symbol.

Context — the bug this fixes:
    Symbol-reference-trace and change-scope-audit mining builds ground truth
    by token-matching the symbol name across the repo (local grep + optional
    Sourcegraph enrichment). For symbols whose name collides with stdlib or
    other packages — e.g. ``MkdirAll``, ``ReadFile``, ``WriteFile`` in Go
    where ``os.MkdirAll`` is stdlib — the result set includes many files that
    do not actually depend on the defining symbol. This inflates the oracle
    file list and rewards agents that blindly grep over agents that reason
    about types.

The invariant: in namespaced/typed languages (Go, Python, Java, TS, …), a
file can reference a symbol from another package only if it (a) is in the
same package as the defining file, or (b) imports the defining package. This
module performs that mechanical check.

ZFC: pure IO + string matching. No type inference, no parsing. Loses some
precision on dot imports and transitive re-exports, but cuts the far larger
stdlib / same-name-different-package false-positive class.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path


def filter_by_import_dependency(
    candidate_files: Iterable[str],
    defining_file: str,
    repo_path: Path,
    language: str,
) -> frozenset[str]:
    """Return the subset of *candidate_files* that can plausibly reference
    the symbol defined at *defining_file*.

    A file is kept when it is in the same package as *defining_file* or it
    imports the package containing *defining_file*. Files we cannot read
    from disk are kept conservatively.

    For unsupported languages (or missing language metadata like ``go.mod``),
    this function returns the input unchanged. Preserving existing behavior
    on unknown shapes is the fail-safe default: false positives are better
    than silently dropping valid references.

    Args:
        candidate_files: Repo-relative paths produced by grep/SG enrichment.
        defining_file: Repo-relative path of the file that defines the symbol.
        repo_path: Absolute path to the repo root.
        language: Language identifier as used elsewhere in mining ("go",
            "python"). Unknown values are a no-op.
    """
    candidates = frozenset(candidate_files)
    if language == "go":
        return _filter_go(candidates, defining_file, repo_path)
    if language == "python":
        return _filter_python(candidates, defining_file, repo_path)
    return candidates


# ---------------------------------------------------------------------------
# Go
# ---------------------------------------------------------------------------


def _go_module_path(repo_path: Path) -> str:
    """Return the Go module path from ``go.mod``, or empty string if absent."""
    go_mod = repo_path / "go.mod"
    if not go_mod.is_file():
        return ""
    try:
        for line in go_mod.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("module "):
                return stripped[len("module ") :].strip()
    except OSError:
        return ""
    return ""


def _go_defining_package_path(repo_path: Path, defining_file: str) -> str:
    """Return the fully-qualified import path of the package containing
    *defining_file*, or empty string if it cannot be resolved."""
    module = _go_module_path(repo_path)
    if not module:
        return ""
    pkg_dir = str(Path(defining_file).parent)
    if pkg_dir in ("", "."):
        return module
    return f"{module}/{pkg_dir}"


def _filter_go(
    candidates: frozenset[str], defining_file: str, repo_path: Path
) -> frozenset[str]:
    pkg_path = _go_defining_package_path(repo_path, defining_file)
    if not pkg_path:
        return candidates
    defining_dir = str(Path(defining_file).parent)
    needle = f'"{pkg_path}"'

    kept: set[str] = set()
    for rel in candidates:
        # Same package (covers the defining file itself and its siblings).
        if str(Path(rel).parent) == defining_dir:
            kept.add(rel)
            continue
        abs_path = repo_path / rel
        if not abs_path.is_file():
            # File unreadable / out-of-tree — preserve conservatively.
            kept.add(rel)
            continue
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            kept.add(rel)
            continue
        if needle in text:
            kept.add(rel)
    return frozenset(kept)


# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------


def _python_defining_module(defining_file: str) -> tuple[str, str]:
    """Return ``(module_dotted, package_dotted)`` for *defining_file*.

    ``"src/core/base_handler.py"`` → ``("src.core.base_handler", "src.core")``.
    Either component may be empty when the path has no directory parent.
    """
    p = Path(defining_file)
    if p.suffix != ".py":
        return "", ""
    parent_parts = [seg for seg in p.parent.parts if seg and seg != "."]
    module = ".".join([*parent_parts, p.stem])
    package = ".".join(parent_parts)
    return module, package


def _filter_python(
    candidates: frozenset[str], defining_file: str, repo_path: Path
) -> frozenset[str]:
    module, package = _python_defining_module(defining_file)
    if not module:
        return candidates
    defining_dir = str(Path(defining_file).parent)

    # Substring patterns — cheap and correct enough for the mining use case.
    # We check for:
    #   - ``from <module>`` (e.g. ``from src.core.base_handler import ...``)
    #   - ``import <module>``
    #   - ``from <package> import <basename>`` (e.g. ``from src.core import base_handler``)
    #   - ``import <package>``
    basename = Path(defining_file).stem
    patterns = [f"from {module}", f"import {module}"]
    if package:
        patterns.append(f"from {package} import {basename}")
        patterns.append(f"import {package}")

    kept: set[str] = set()
    for rel in candidates:
        if str(Path(rel).parent) == defining_dir:
            kept.add(rel)
            continue
        abs_path = repo_path / rel
        if not abs_path.is_file():
            kept.add(rel)
            continue
        try:
            text = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            kept.add(rel)
            continue
        if any(pat in text for pat in patterns):
            kept.add(rel)
    return frozenset(kept)
