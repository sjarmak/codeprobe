"""Symbol resolution helpers used by :func:`check_oracle_coherence`.

The lib's contract is "best effort": prefer ``ast-grep`` (precise, AST-aware)
when it's on ``$PATH``, otherwise fall back to a simple regex that matches the
identifier as a word boundary inside the file. The fallback is intentionally
conservative — it can produce false positives for symbols that share names
with comments or string literals, but it never produces false negatives, which
is the correct asymmetry for a QA check whose escalation path is "warn the
human, then they look".
"""

from __future__ import annotations

import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def _astgrep_available() -> bool:
    """Return ``True`` when ``ast-grep`` is callable on ``$PATH``."""
    return shutil.which("ast-grep") is not None or shutil.which("sg") is not None


def _astgrep_binary() -> str | None:
    """Resolve the binary name for ast-grep (``ast-grep`` or its ``sg`` alias)."""
    if shutil.which("ast-grep"):
        return "ast-grep"
    if shutil.which("sg"):
        return "sg"
    return None


def symbol_in_file(
    file_path: Path,
    symbol: str,
    *,
    prefer_astgrep: bool = True,
) -> bool:
    """Return ``True`` if ``symbol`` appears as an identifier in ``file_path``.

    Tries ``ast-grep`` first when it's available and ``prefer_astgrep`` is
    ``True``; falls back to a word-boundary regex search otherwise. Reads the
    file as UTF-8 with ``errors="replace"`` so binary or oddly-encoded files
    don't crash the check (they just won't match).

    Args:
        file_path: Absolute or relative path to the file. Must exist; callers
            are expected to have verified existence via the file-existence
            check first.
        symbol: Identifier to look for. Treated as a literal string for both
            backends.
        prefer_astgrep: Use ast-grep when available. Set ``False`` to force
            the regex fallback (useful in tests).

    Returns:
        ``True`` if the symbol is found, ``False`` otherwise. Returns
        ``False`` when the file can't be read.
    """
    if prefer_astgrep and _astgrep_available():
        binary = _astgrep_binary()
        if binary is not None:
            result = _run_astgrep(binary, file_path, symbol)
            if result is not None:
                return result
    return _regex_search(file_path, symbol)


def _run_astgrep(binary: str, file_path: Path, symbol: str) -> bool | None:
    """Run ast-grep with the symbol as a literal pattern.

    Returns ``True`` / ``False`` on success, or ``None`` to signal that the
    caller should use the regex fallback (e.g. ast-grep crashed, timed out,
    or returned a non-zero status that doesn't simply mean "no match").
    """
    try:
        proc = subprocess.run(
            [binary, "run", "--pattern", symbol, str(file_path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode == 0:
        return bool(proc.stdout.strip())
    if proc.returncode == 1 and not proc.stderr.strip():
        return False
    return None


def _regex_search(file_path: Path, symbol: str) -> bool:
    """Word-boundary regex search; conservative fallback for ast-grep."""
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    pattern = re.compile(rf"\b{re.escape(symbol)}\b")
    return bool(pattern.search(text))
