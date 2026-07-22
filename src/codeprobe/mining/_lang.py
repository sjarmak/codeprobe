"""Unified language detection from file extensions."""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

#: Languages the mining pipeline can emit real test commands for.
#: Everything else fails fast with UNSUPPORTED_LANGUAGE (locked decision 5).
SUPPORTED_MINING_LANGUAGES: frozenset[str] = frozenset(
    {"python", "go", "javascript", "typescript"}
)

_EXT_TO_LANGUAGE: dict[str, str] = {
    ".c": "c",
    ".cpp": "cpp",
    ".cs": "csharp",
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


# Directories skipped by the non-git fallback walk (mirrors the suitability
# check in cli/mine_cmd.py — VCS metadata and vendored/build detritus say
# nothing about the repo's primary language).
_DETECT_SKIP_DIRS: frozenset[str] = frozenset(
    {".git", "node_modules", "vendor", ".venv", "venv", "dist", "build"}
)

#: File-sample cap for language detection — enough for a majority vote
#: without walking a monorepo's whole tree.
_DETECT_FILE_CAP = 2000


def detect_repo_language(repo_path: Path) -> str:
    """Detect the primary language of *repo_path* from its file paths.

    Prefers ``git ls-files`` (respects .gitignore and sees exactly the
    tracked tree); on git failure or an empty index falls back to a bounded
    directory walk that skips VCS/vendor/venv/build detritus. Returns
    ``"unknown"`` for empty or undetectable repositories (e.g. docs-only).
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        proc = None

    if proc is not None and proc.returncode == 0:
        tracked = frozenset(line for line in proc.stdout.splitlines() if line)
        if tracked:
            return guess_language_from_paths(tracked, limit=_DETECT_FILE_CAP)

    # Fallback: bounded walk for non-git dirs and empty indexes.
    extensions: list[str] = []
    try:
        for p in repo_path.rglob("*"):
            if _DETECT_SKIP_DIRS & set(p.parts):
                continue
            if p.is_file() and p.suffix:
                extensions.append(p.suffix)
            if len(extensions) >= _DETECT_FILE_CAP:
                break
    except OSError:
        return "unknown"
    return guess_language_from_extensions(extensions)
