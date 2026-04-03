"""Unified language detection from file extensions."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

_EXT_TO_LANGUAGE: dict[str, str] = {
    ".c": "c",
    ".cpp": "cpp",
    ".go": "go",
    ".java": "java",
    ".js": "javascript",
    ".kt": "kotlin",
    ".php": "php",
    ".py": "python",
    ".rb": "ruby",
    ".rs": "rust",
    ".swift": "swift",
    ".ts": "typescript",
}


def ext_to_language(suffix: str) -> str:
    """Map a single file extension to a language name, or ``"unknown"``."""
    return _EXT_TO_LANGUAGE.get(suffix, "unknown")


def guess_language_from_extensions(extensions: list[str]) -> str:
    """Guess primary language from a list of file extensions (e.g. ``[".py", ".py", ".go"]``)."""
    counts = Counter(
        _EXT_TO_LANGUAGE[ext] for ext in extensions if ext in _EXT_TO_LANGUAGE
    )
    if not counts:
        return "unknown"
    return counts.most_common(1)[0][0]


def guess_language_from_paths(paths: frozenset[str], *, limit: int = 1000) -> str:
    """Guess primary language from a set of file paths (samples up to *limit*)."""
    extensions = [Path(f).suffix for f in list(paths)[:limit] if Path(f).suffix]
    return guess_language_from_extensions(extensions)
