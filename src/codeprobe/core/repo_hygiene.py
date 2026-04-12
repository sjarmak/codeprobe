"""Ensure target repos have .codeprobe/ in .git/info/exclude.

Prevents accidental commits of codeprobe benchmark state (mined tasks,
experiment configs, run results) into upstream repositories.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_EXCLUDE_ENTRY = ".codeprobe/"
_COMMENT = "# codeprobe: local benchmark state, never commit upstream"


def ensure_codeprobe_excluded(repo_path: Path) -> None:
    """Idempotently add ``.codeprobe/`` to ``<repo>/.git/info/exclude``.

    - If *repo_path* is not a git repository, this is a silent no-op.
    - If ``.codeprobe/`` is already listed, this is a no-op.
    - Creates ``.git/info/`` and ``exclude`` if they don't exist.
    """
    git_dir = repo_path / ".git"
    if not git_dir.is_dir():
        return

    info_dir = git_dir / "info"
    info_dir.mkdir(parents=True, exist_ok=True)

    exclude_file = info_dir / "exclude"

    if exclude_file.is_file():
        content = exclude_file.read_text(encoding="utf-8")
        # Check for exact line match to avoid false positives
        lines = content.splitlines()
        if any(line.strip() == _EXCLUDE_ENTRY for line in lines):
            return
    else:
        content = ""

    suffix = ""
    if content and not content.endswith("\n"):
        suffix = "\n"

    exclude_file.write_text(
        f"{content}{suffix}{_COMMENT}\n{_EXCLUDE_ENTRY}\n",
        encoding="utf-8",
    )
    logger.info("Added %s to %s", _EXCLUDE_ENTRY, exclude_file)
