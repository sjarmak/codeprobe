"""Commit narrative adapter — returns the full commit message body."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from codeprobe.mining.sources import NarrativeBundle

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 10
_MAX_BODY_CHARS = 50_000


@dataclass(frozen=True)
class CommitAdapter:
    """Read the raw commit message for a SHA via ``git log -n 1 --format=%B``.

    Accepts any message length — even a one-line squash commit becomes a
    valid :class:`NarrativeBundle`. The INV1 guarantee (loud error for
    squash-only repos) lives at the CLI layer; this adapter's job is to
    faithfully surface what git has when the user has explicitly asked
    for commit messages.
    """

    name: str = field(default="commits")

    def fetch(self, repo: Path, commit_sha: str) -> NarrativeBundle | None:
        try:
            result = subprocess.run(
                ["git", "log", "-n", "1", "--format=%B", commit_sha],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            logger.debug(
                "CommitAdapter: git log failed for %s: %s", commit_sha[:8], exc
            )
            return None

        if result.returncode != 0:
            logger.debug(
                "CommitAdapter: git log rc=%d for %s: %s",
                result.returncode,
                commit_sha[:8],
                result.stderr.strip(),
            )
            return None

        message = (result.stdout or "").strip()
        if not message:
            return None

        return NarrativeBundle(
            text=message[:_MAX_BODY_CHARS],
            metadata={"sha": commit_sha},
            source_name=self.name,
        )
