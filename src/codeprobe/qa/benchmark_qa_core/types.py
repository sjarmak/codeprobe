"""Core record and constraint types for benchmark_qa_core checks.

These are deliberately small, frozen dataclasses so callers can compare,
serialise, and aggregate findings without worrying about identity or mutation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class Finding:
    """A single QA finding produced by a check function.

    Attributes:
        severity: ``error`` for hard contract violations that should fail the
            task in CI, ``warning`` for likely-but-not-certain problems, and
            ``info`` for advisory observations.
        code: A short, stable identifier (e.g. ``"A1"``, ``"E2"``) that callers
            can pin in waivers or downstream gating. Codes never re-use across
            check functions; see the package docstring for the namespace.
        message: Human-readable description of the issue. Should not embed
            ANSI escapes — formatting is the caller's concern.
        location: Where the issue lives (file path, symbol id, JSON pointer
            into task-meta, …). ``None`` when the finding is global to the
            task or not tied to one location.
        suggested_fix: Optional one-line hint about how to resolve the
            finding. Free-form text; the lib does not parse it.
    """

    severity: Severity
    code: str
    message: str
    location: str | None = None
    suggested_fix: str | None = None


@dataclass(frozen=True)
class OracleConstraints:
    """Knobs that scope :func:`check_oracle_coherence`.

    Attributes:
        expected_languages: Set of language tokens (e.g. ``{"python"}``,
            ``{"typescript", "javascript"}``) that oracle file extensions must
            map into. Empty set disables the language-match check.
        path_include: Glob patterns; if non-empty, every oracle file path must
            match at least one. Patterns use :mod:`fnmatch` semantics relative
            to the repo root.
        path_exclude: Glob patterns; if any oracle file matches one, that file
            triggers a ``D2`` finding even if it also matches ``path_include``
            (exclude wins).
        require_symbols_resolve: When ``True``, every entry in
            ``oracle_symbols`` must be locatable in its referenced file
            (best-effort regex search, optionally upgraded to ast-grep when
            installed). When ``False``, missing symbols downgrade to a
            warning. Default ``True``.
        language_extensions: Optional override mapping from extension (with
            leading dot, lower-cased) to language token. Defaults to a small
            built-in table covering the common cases used by codeprobe / EB /
            CSB. Callers can extend the table for niche languages.
    """

    expected_languages: frozenset[str] = field(default_factory=frozenset)
    path_include: tuple[str, ...] = ()
    path_exclude: tuple[str, ...] = ()
    require_symbols_resolve: bool = True
    language_extensions: dict[str, str] | None = None


DEFAULT_LANGUAGE_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".swift": "swift",
    ".rb": "ruby",
    ".php": "php",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".c": "c",
    ".h": "c",
    ".cs": "csharp",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
}
