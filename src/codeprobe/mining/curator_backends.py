"""Pluggable curation backends for the curator pipeline.

Four implementations of the CurationBackend Protocol:
  1. GrepBackend — wraps scan_repo_for_family() for local grep-based scanning
  2. SourcegraphBackend — Sourcegraph GraphQL API search
  3. PRDiffBackend — git log diff-filter for recently modified files
  4. AgentSearchBackend — LLM-based file identification via Haiku

ZFC compliant: mechanism only (IO, structural validation, arithmetic).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

from codeprobe.core.llm import LLMError, LLMRequest, call_claude, llm_available
from codeprobe.mining.curator import CuratedFile
from codeprobe.mining.org_scale_families import TaskFamily
from codeprobe.mining.org_scale_scanner import (
    FamilyScanResult,
    matches_glob,
    scan_repo_for_family,
)

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 30
_MAX_FILE_SIZE_CONTENT_CHECK = 512_000  # Skip files larger than 512KB
_MAX_LINE_LEN_CONTENT_CHECK = 500


def _compile_content_patterns(
    patterns: tuple[str, ...],
) -> list[re.Pattern[str]]:
    """Compile content regex patterns, skipping invalid ones."""
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p))
        except re.error:
            pass
    return compiled


def _file_matches_content(
    full_path: Path,
    compiled_patterns: list[re.Pattern[str]],
) -> bool:
    """Check if a file contains at least one content pattern match."""
    try:
        if full_path.stat().st_size > _MAX_FILE_SIZE_CONTENT_CHECK:
            return False
        content = full_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in content.splitlines():
        if len(line) > _MAX_LINE_LEN_CONTENT_CHECK:
            continue
        for pat in compiled_patterns:
            if pat.search(line):
                return True
    return False


# ---------------------------------------------------------------------------
# 1. GrepBackend
# ---------------------------------------------------------------------------


class GrepBackend:
    """Wraps scan_repo_for_family() and converts hits to CuratedFile list."""

    @property
    def name(self) -> str:
        return "grep"

    def available(self) -> bool:
        return True

    def search(
        self,
        repos: list[Path],
        family: TaskFamily,
    ) -> list[CuratedFile]:
        result: FamilyScanResult = scan_repo_for_family(repos, family)

        # Count hits per file for hit_count
        file_hit_counts: Counter[str] = Counter()
        file_lines: dict[str, list[int]] = {}
        for hit in result.hits:
            file_hit_counts[hit.file_path] += 1
            file_lines.setdefault(hit.file_path, []).append(hit.line_number)

        curated: list[CuratedFile] = []
        for fp in sorted(result.matched_files):
            curated.append(
                CuratedFile(
                    path=fp,
                    tier="required",
                    sources=("grep",),
                    confidence=1.0,
                    hit_count=file_hit_counts.get(fp, 1),
                    line_matches=tuple(sorted(file_lines.get(fp, []))),
                )
            )
        return curated


# ---------------------------------------------------------------------------
# 2. SourcegraphBackend
# ---------------------------------------------------------------------------

_SG_MAX_RETRIES = 6
_SG_INITIAL_BACKOFF = 1  # seconds; doubles each retry (2^n pattern)


class SourcegraphBackend:
    """Sourcegraph GraphQL API search backend.

    Requires SOURCEGRAPH_ENDPOINT and SOURCEGRAPH_TOKEN env vars.
    Retries up to 6 times with exponential backoff on HTTP 429.
    """

    @property
    def name(self) -> str:
        return "sourcegraph"

    def available(self) -> bool:
        return os.environ.get("SOURCEGRAPH_TOKEN") is not None

    def search(
        self,
        repos: list[Path],
        family: TaskFamily,
    ) -> list[CuratedFile]:
        endpoint = os.environ.get("SOURCEGRAPH_ENDPOINT", "")
        token = os.environ.get("SOURCEGRAPH_TOKEN", "")
        if not endpoint or not token:
            logger.warning("Sourcegraph endpoint or token not configured")
            return []

        curated: list[CuratedFile] = []
        for repo_path in repos:
            repo_name = repo_path.name
            for pattern in family.content_patterns:
                query = self._build_query(repo_name, pattern)
                files = self._execute_query(endpoint, token, query)
                for fp in files:
                    curated.append(
                        CuratedFile(
                            path=fp,
                            tier="required",
                            sources=("sourcegraph",),
                            confidence=0.9,
                            hit_count=1,
                        )
                    )
        # Deduplicate by path (keep first occurrence)
        seen: set[str] = set()
        deduped: list[CuratedFile] = []
        for cf in curated:
            if cf.path not in seen:
                seen.add(cf.path)
                deduped.append(cf)
        return deduped

    def _build_query(self, repo_name: str, pattern: str) -> str:
        return f"repo:^{re.escape(repo_name)}$ patterntype:regexp {pattern}"

    def _execute_query(
        self,
        endpoint: str,
        token: str,
        query: str,
    ) -> list[str]:
        graphql_query = json.dumps(
            {
                "query": (
                    "query($q: String!) { search(query: $q) "
                    "{ results { results { ... on FileMatch "
                    "{ file { path } } } } } }"
                ),
                "variables": {"q": query},
            }
        )

        headers = {
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
        }

        for attempt in range(_SG_MAX_RETRIES):
            req = urllib.request.Request(
                endpoint,
                data=graphql_query.encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    return self._parse_response(data)
            except urllib.error.HTTPError as exc:
                if exc.code == 429:
                    backoff = 2**attempt * _SG_INITIAL_BACKOFF
                    logger.warning(
                        "Sourcegraph rate limited (429), retrying in %ds "
                        "(attempt %d/%d)",
                        backoff,
                        attempt + 1,
                        _SG_MAX_RETRIES,
                    )
                    time.sleep(backoff)
                    continue
                logger.warning("Sourcegraph HTTP error %d: %s", exc.code, exc.reason)
                return []
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                logger.warning("Sourcegraph request failed: %s", exc)
                return []

        logger.warning("Sourcegraph: exhausted %d retries", _SG_MAX_RETRIES)
        return []

    def _parse_response(self, data: dict) -> list[str]:
        try:
            results = data["data"]["search"]["results"]["results"]
            return [r["file"]["path"] for r in results if "file" in r]
        except (KeyError, TypeError):
            return []


# ---------------------------------------------------------------------------
# 3. PRDiffBackend
# ---------------------------------------------------------------------------


class PRDiffBackend:
    """Finds recently modified files via git log, filtered by family globs."""

    @property
    def name(self) -> str:
        return "pr_diff"

    def available(self) -> bool:
        return True

    def search(
        self,
        repos: list[Path],
        family: TaskFamily,
    ) -> list[CuratedFile]:
        curated: list[CuratedFile] = []
        seen: set[str] = set()

        compiled_patterns = _compile_content_patterns(family.content_patterns)

        for repo_path in repos:
            modified_files = self._get_modified_files(repo_path)
            for fp in modified_files:
                if fp in seen:
                    continue
                if not any(matches_glob(fp, g) for g in family.glob_patterns):
                    continue
                # Also verify at least one content pattern matches
                if compiled_patterns and not _file_matches_content(
                    repo_path / fp, compiled_patterns
                ):
                    continue
                seen.add(fp)
                curated.append(
                    CuratedFile(
                        path=fp,
                        tier="required",
                        sources=("pr_diff",),
                        confidence=0.7,
                        hit_count=1,
                    )
                )
        return curated

    def _get_modified_files(self, repo_path: Path) -> list[str]:
        try:
            result = subprocess.run(
                [
                    "git",
                    "log",
                    "--diff-filter=M",
                    "--name-only",
                    "--since=6months",
                    "--format=",
                ],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
            if result.returncode != 0:
                return []
            # Deduplicate and filter empty lines
            files: list[str] = []
            seen: set[str] = set()
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if line and line not in seen:
                    seen.add(line)
                    files.append(line)
            return files
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("git log failed for %s: %s", repo_path, exc)
            return []


# ---------------------------------------------------------------------------
# 4. AgentSearchBackend
# ---------------------------------------------------------------------------

_MAX_FILE_LISTING = 2000


class AgentSearchBackend:
    """LLM-based file identification using Haiku model.

    Sends a capped file listing plus family description to Haiku and
    asks the model to identify relevant files.
    """

    @property
    def name(self) -> str:
        return "agent_search"

    def available(self) -> bool:
        return llm_available()

    def search(
        self,
        repos: list[Path],
        family: TaskFamily,
    ) -> list[CuratedFile]:
        all_files = self._collect_files(repos, family)

        # Cap at _MAX_FILE_LISTING entries
        capped = all_files[:_MAX_FILE_LISTING]
        if not capped:
            return []

        file_listing = "\n".join(capped)
        prompt = (
            f"You are a code analysis assistant. Given the following file listing "
            f"from a codebase, identify files that are relevant to this task family:\n\n"
            f"**Family:** {family.name}\n"
            f"**Description:** {family.description}\n"
            f"**Content patterns:** {', '.join(family.content_patterns)}\n\n"
            f"**File listing ({len(capped)} files):**\n"
            f"{file_listing}\n\n"
            f"Return ONLY a JSON array of file paths that are likely relevant. "
            f"No explanation, no markdown fences, just a JSON array of strings."
        )

        try:
            response = call_claude(
                LLMRequest(prompt=prompt, model="haiku", timeout_seconds=30)
            )
            return self._parse_response(response.text, capped)
        except LLMError as exc:
            logger.warning("AgentSearch LLM call failed: %s", exc)
            return []

    def _collect_files(
        self,
        repos: list[Path],
        family: TaskFamily,
    ) -> list[str]:
        """Collect files from repos, sorted by glob pattern relevance."""
        from codeprobe.mining.org_scale_scanner import get_tracked_files

        matching: list[str] = []
        non_matching: list[str] = []

        for repo_path in repos:
            tracked = get_tracked_files(repo_path)
            for fp in sorted(tracked):
                if any(matches_glob(fp, g) for g in family.glob_patterns):
                    matching.append(fp)
                else:
                    non_matching.append(fp)

        # Glob-matching files first, then others
        return matching + non_matching

    def _parse_response(
        self,
        text: str,
        valid_files: list[str],
    ) -> list[CuratedFile]:
        """Parse LLM response as JSON array, validate against known files."""
        # Try to extract JSON array from response
        text = text.strip()
        # Handle markdown fences
        if text.startswith("```"):
            lines = text.splitlines()
            text = "\n".join(
                line for line in lines if not line.strip().startswith("```")
            )

        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("AgentSearch: failed to parse LLM response as JSON")
            return []

        if not isinstance(parsed, list):
            return []

        valid_set = frozenset(valid_files)
        curated: list[CuratedFile] = []
        for item in parsed:
            if isinstance(item, str) and item in valid_set:
                curated.append(
                    CuratedFile(
                        path=item,
                        tier="required",
                        sources=("agent_search",),
                        confidence=0.6,
                        hit_count=1,
                    )
                )
        return curated
