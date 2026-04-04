"""Curator core: protocols, data models, and quorum-based merge pipeline.

Provides the CurationBackend Protocol, CuratedFile / MergeConfig / CurationResult
frozen dataclasses, a merge_results() function implementing quorum-based merge
with configurable min_backends and min_confidence, and a CurationPipeline
orchestrator class.

ZFC compliant: mechanism only (IO, structural validation, arithmetic).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from codeprobe.mining.org_scale_families import TaskFamily

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CuratedFile:
    """A single curated file with provenance and confidence metadata."""

    path: str
    tier: str = "required"
    sources: tuple[str, ...] = ()
    confidence: float = 1.0
    hit_count: int = 1
    line_matches: tuple[int, ...] = ()


@dataclass(frozen=True)
class MergeConfig:
    """Configuration for quorum-based merge of backend results."""

    min_backends: int = 1
    min_confidence: float = 0.3
    backend_weights: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class CurationResult:
    """Result of curating files for a task family across repos."""

    family: TaskFamily
    files: tuple[CuratedFile, ...]
    repo_paths: tuple[Path, ...]
    commit_shas: dict[str, str]
    backends_used: tuple[str, ...]
    merge_config: MergeConfig
    matched_files: frozenset[str] = frozenset()
    verification_result: str | None = None

    @classmethod
    def from_scan_result(
        cls,
        scan_result: object,
    ) -> CurationResult:
        """Create a CurationResult from a FamilyScanResult.

        Bridges the scanner output into the curator data model (Risk 1 mitigation).
        Accepts ``object`` to avoid a hard import cycle at the type level; the
        actual runtime type is ``FamilyScanResult``.
        """
        # Deferred import to avoid circular dependency at module level.
        from codeprobe.mining.org_scale_scanner import FamilyScanResult

        if not isinstance(scan_result, FamilyScanResult):
            msg = f"Expected FamilyScanResult, got {type(scan_result).__name__}"
            raise TypeError(msg)

        # Collect per-file line numbers from PatternHit objects.
        file_lines: dict[str, list[int]] = {}
        for hit in scan_result.hits:
            file_lines.setdefault(hit.file_path, []).append(hit.line_number)

        curated_files = tuple(
            CuratedFile(
                path=fp,
                tier="required",
                sources=("grep",),
                confidence=1.0,
                hit_count=len(file_lines.get(fp, [])) or 1,
                line_matches=tuple(sorted(file_lines.get(fp, []))),
            )
            for fp in sorted(scan_result.matched_files)
        )

        # Build commit_shas dict: use repo directory name as key.
        commit_shas: dict[str, str] = {}
        sha_parts = scan_result.commit_sha.split(",")
        for idx, repo_path in enumerate(scan_result.repo_paths):
            if idx < len(sha_parts):
                commit_shas[repo_path.name] = sha_parts[idx]

        return cls(
            family=scan_result.family,
            files=curated_files,
            repo_paths=scan_result.repo_paths,
            commit_shas=commit_shas,
            backends_used=("grep",),
            merge_config=MergeConfig(),
            matched_files=scan_result.matched_files,
        )


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class CurationBackend(Protocol):
    """Protocol for pluggable curation backends (grep, AST, LLM, etc.)."""

    @property
    def name(self) -> str: ...  # pragma: no cover

    def search(
        self,
        repos: list[Path],
        family: TaskFamily,
    ) -> list[CuratedFile]: ...  # pragma: no cover

    def available(self) -> bool: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------


def merge_results(
    backend_results: dict[str, list[CuratedFile]],
    config: MergeConfig,
) -> tuple[CuratedFile, ...]:
    """Merge results from multiple backends using quorum-based dedup.

    For each unique file path:
    - Computes weighted-average confidence across backends.
    - Sums hit_counts from all backends.
    - Collects all sources.
    - Filters by min_backends and min_confidence.
    """
    if not backend_results:
        return ()

    # Collect per-path entries grouped by backend name.
    per_path: dict[str, list[tuple[str, CuratedFile]]] = {}
    for backend_name, files in backend_results.items():
        for cf in files:
            per_path.setdefault(cf.path, []).append((backend_name, cf))

    # Directories excluded from ground truth (same as scanner)
    _EXCLUDED_DIRS = ("vendor/", "node_modules/", "testdata/")

    merged: list[CuratedFile] = []
    for path, entries in sorted(per_path.items()):
        # Skip vendored / generated / test-data paths
        if any(seg in path for seg in _EXCLUDED_DIRS):
            continue
        # Quorum filter: require min_backends distinct backends.
        distinct_backends = {name for name, _ in entries}
        if len(distinct_backends) < config.min_backends:
            continue

        # Weighted-average confidence.
        total_weight = 0.0
        weighted_conf = 0.0
        total_hits = 0
        all_sources: list[str] = []
        all_lines: list[int] = []
        tier = "required"

        for backend_name, cf in entries:
            weight = config.backend_weights.get(backend_name, 1.0)
            total_weight += weight
            weighted_conf += cf.confidence * weight
            total_hits += cf.hit_count
            all_sources.extend(cf.sources)
            all_lines.extend(cf.line_matches)
            # Keep the most specific tier (first non-default wins).
            if cf.tier != "required":
                tier = cf.tier

        avg_confidence = weighted_conf / total_weight if total_weight > 0 else 0.0

        # Confidence filter.
        if avg_confidence < config.min_confidence:
            continue

        # Deduplicate sources while preserving order.
        seen_sources: set[str] = set()
        unique_sources: list[str] = []
        for s in all_sources:
            if s not in seen_sources:
                seen_sources.add(s)
                unique_sources.append(s)

        merged.append(
            CuratedFile(
                path=path,
                tier=tier,
                sources=tuple(unique_sources),
                confidence=avg_confidence,
                hit_count=total_hits,
                line_matches=tuple(sorted(set(all_lines))),
            )
        )

    return tuple(merged)


# ---------------------------------------------------------------------------
# Pipeline orchestrator
# ---------------------------------------------------------------------------


class CurationPipeline:
    """Orchestrates multiple CurationBackend instances and merges results."""

    def __init__(
        self,
        backends: list[CurationBackend],
        config: MergeConfig | None = None,
    ) -> None:
        self._backends = backends
        self._config = config or MergeConfig()

    def curate(
        self,
        repos: list[Path],
        family: TaskFamily,
        *,
        commit_shas: dict[str, str] | None = None,
    ) -> CurationResult:
        """Run all available backends and merge results."""
        available = [b for b in self._backends if b.available()]
        if not available:
            logger.warning("No backends available for family %s", family.name)

        backend_results: dict[str, list[CuratedFile]] = {}
        for backend in available:
            try:
                results = backend.search(repos, family)
                backend_results[backend.name] = results
                logger.info(
                    "Backend %s returned %d files for %s",
                    backend.name,
                    len(results),
                    family.name,
                )
            except Exception:
                logger.exception(
                    "Backend %s failed for family %s",
                    backend.name,
                    family.name,
                )

        merged = merge_results(backend_results, self._config)
        matched = frozenset(cf.path for cf in merged)

        return CurationResult(
            family=family,
            files=merged,
            repo_paths=tuple(repos),
            commit_shas=commit_shas or {},
            backends_used=tuple(sorted(backend_results.keys())),
            merge_config=self._config,
            matched_files=matched,
        )
