"""RFC narrative adapter — surfaces design docs from conventional locations.

Looks for Markdown files under ``docs/rfcs/``, ``rfcs/``, ``docs/adr/``,
or ``docs/design/``. Prefers RFCs touched in the commit's diff; otherwise
falls back to the most-recently-modified RFC in the repo.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from codeprobe.mining.sources import NarrativeBundle

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 10
_MAX_RFC_CHARS = 16_000

# Relative directories where RFC / ADR / design docs conventionally live.
_RFC_DIRS: tuple[tuple[str, ...], ...] = (
    ("docs", "rfcs"),
    ("rfcs",),
    ("docs", "adr"),
    ("docs", "design"),
    ("doc", "rfcs"),
)


@dataclass(frozen=True)
class RFCAdapter:
    """Fetch the most-relevant RFC/ADR markdown file for a commit.

    Returns ``None`` when the repo has no RFC directory at all — a hard
    signal that lets the caller try the next adapter or fail loudly.
    """

    name: str = field(default="rfcs")

    def fetch(self, repo: Path, commit_sha: str) -> NarrativeBundle | None:
        rfc_dirs = [repo.joinpath(*parts) for parts in _RFC_DIRS]
        existing = [d for d in rfc_dirs if d.is_dir()]
        if not existing:
            return None

        # Priority 1: RFC files touched in this commit's diff.
        touched = self._commit_touched_rfcs(repo, commit_sha, existing)
        if touched:
            chosen = touched[0]
            return self._read_bundle(repo, chosen)

        # Priority 2: most-recently-modified RFC anywhere in the RFC dirs.
        candidates: list[Path] = []
        for d in existing:
            candidates.extend(p for p in d.rglob("*.md") if p.is_file())
        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return self._read_bundle(repo, candidates[0])

    @staticmethod
    def _commit_touched_rfcs(
        repo: Path, commit_sha: str, rfc_dirs: list[Path]
    ) -> list[Path]:
        try:
            result = subprocess.run(
                ["git", "show", "--name-only", "--format=", commit_sha],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return []
        if result.returncode != 0:
            return []

        rfc_roots = {d.resolve() for d in rfc_dirs}
        touched: list[Path] = []
        for rel in result.stdout.splitlines():
            rel = rel.strip()
            if not rel or not rel.lower().endswith(".md"):
                continue
            candidate = (repo / rel).resolve()
            if not candidate.is_file():
                continue
            if any(root in candidate.parents for root in rfc_roots):
                touched.append(candidate)
        return touched

    def _read_bundle(self, repo: Path, path: Path) -> NarrativeBundle:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.debug("RFCAdapter: failed to read %s: %s", path, exc)
            text = ""
        try:
            rel = str(path.resolve().relative_to(repo.resolve()))
        except ValueError:
            rel = str(path)
        return NarrativeBundle(
            text=text[:_MAX_RFC_CHARS],
            metadata={"rfc_path": rel},
            source_name=self.name,
        )
