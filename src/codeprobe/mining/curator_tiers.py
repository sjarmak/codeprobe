"""Tier classification and curation verification for curated file sets.

Provides two functions:
- classify_tiers(): Assigns tier labels (required/supplementary/context)
  to curated files using LLM judgment or a source-count heuristic.
- verify_curation(): Samples files and asks an LLM to confirm curation
  membership, returning pass/warn/fail.

ZFC compliant: LLM handles semantic classification; code does IO and
structural parsing only.
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import replace
from pathlib import Path

from codeprobe.core.llm import (
    LLMError,
    LLMRequest,
    call_claude,
    llm_available,
)
from codeprobe.mining.curator import CuratedFile
from codeprobe.mining.org_scale_families import TaskFamily
from codeprobe.mining.org_scale_scanner import get_tracked_files

logger = logging.getLogger(__name__)

_VALID_TIERS = frozenset({"required", "supplementary", "context"})


# ---------------------------------------------------------------------------
# classify_tiers
# ---------------------------------------------------------------------------


def _build_classify_prompt(
    files: list[CuratedFile],
    family: TaskFamily,
) -> str:
    """Build the LLM prompt for tier classification."""
    file_list = "\n".join(f"- {f.path}" for f in files)
    return (
        "You are classifying files for relevance to a task family.\n"
        "Use temperature=0 for maximum reproducibility.\n\n"
        f"Task family: {family.name}\n"
        f"Description: {family.description}\n\n"
        "Files to classify:\n"
        f"{file_list}\n\n"
        "For each file, assign exactly one tier:\n"
        '- "required": core files that must be read to complete tasks in this family\n'
        '- "supplementary": helpful but not essential files\n'
        '- "context": background files that provide general context only\n\n'
        "Respond with ONLY a JSON object mapping each file path to its tier.\n"
        'Example: {"src/foo.py": "required", "src/bar.py": "supplementary"}\n'
        "No markdown fences. No explanation. Just the JSON object."
    )


def _classify_heuristic(files: list[CuratedFile]) -> list[CuratedFile]:
    """Classify tiers using source-count heuristic (--no-llm fallback)."""
    result: list[CuratedFile] = []
    for f in files:
        if len(f.sources) >= 2:
            tier = "required"
        elif len(f.sources) == 1:
            tier = "supplementary"
        else:
            tier = "context"
        result.append(replace(f, tier=tier))
    return result


def _parse_tier_response(
    text: str,
    files: list[CuratedFile],
) -> dict[str, str]:
    """Parse LLM JSON response into a path->tier mapping.

    Raises ``ValueError`` if the response cannot be parsed or contains
    invalid tiers.
    """
    # Strip markdown fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first and last fence lines
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        mapping = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Failed to parse tier JSON: {exc}") from exc

    if not isinstance(mapping, dict):
        raise ValueError(f"Expected JSON object, got {type(mapping).__name__}")

    # Validate tiers
    for path, tier in mapping.items():
        if tier not in _VALID_TIERS:
            raise ValueError(f"Invalid tier {tier!r} for {path}")

    return mapping


def classify_tiers(
    files: list[CuratedFile],
    family: TaskFamily,
    repos: list[Path],
    *,
    use_llm: bool = True,
) -> list[CuratedFile]:
    """Classify curated files into tiers (required/supplementary/context).

    With LLM (default): sends all files in one batch prompt to Haiku.
    Without LLM: applies heuristic based on source count.

    Returns a new list of CuratedFile with updated tier field (immutable).
    """
    if not files:
        return []

    if not use_llm:
        return _classify_heuristic(files)

    # LLM path
    prompt = _build_classify_prompt(files, family)
    try:
        # Scale timeout with file count — 30s base + 0.5s per file
        timeout = min(30 + len(files) // 2, 120)
        response = call_claude(
            LLMRequest(prompt=prompt, model="haiku", timeout_seconds=timeout)
        )
        mapping = _parse_tier_response(response.text, files)
    except (LLMError, ValueError) as exc:
        logger.warning(
            "LLM tier classification failed, falling back to heuristic: %s", exc
        )
        return _classify_heuristic(files)

    # Apply tiers from LLM response, preserving heuristic for unmapped files
    result: list[CuratedFile] = []
    for f in files:
        tier = mapping.get(f.path, None)
        if tier and tier in _VALID_TIERS:
            result.append(replace(f, tier=tier))
        else:
            # Unmapped file: use heuristic
            if len(f.sources) >= 2:
                fallback_tier = "required"
            elif len(f.sources) == 1:
                fallback_tier = "supplementary"
            else:
                fallback_tier = "context"
            result.append(replace(f, tier=fallback_tier))

    return result


# ---------------------------------------------------------------------------
# verify_curation
# ---------------------------------------------------------------------------


def _build_verify_prompt(
    in_set: list[str],
    not_in_set: list[str],
    family: TaskFamily,
) -> str:
    """Build the LLM prompt for curation verification."""
    in_list = "\n".join(f"- {p}" for p in in_set)
    not_list = "\n".join(f"- {p}" for p in not_in_set)
    return (
        "You are verifying file curation for a task family.\n"
        "Use temperature=0 for maximum reproducibility.\n\n"
        f"Task family: {family.name}\n"
        f"Description: {family.description}\n\n"
        "FILES CURRENTLY IN THE CURATED SET (should be relevant):\n"
        f"{in_list}\n\n"
        "FILES NOT IN THE CURATED SET (should be irrelevant):\n"
        f"{not_list}\n\n"
        "For each file, respond with whether you AGREE or DISAGREE with its "
        "current classification (in-set or not-in-set).\n\n"
        "Respond with ONLY a JSON object mapping each file path to "
        '"agree" or "disagree".\n'
        'Example: {"src/foo.py": "agree", "src/bar.py": "disagree"}\n'
        "No markdown fences. No explanation. Just the JSON object."
    )


def _parse_verify_response(text: str) -> dict[str, str]:
    """Parse LLM verification response into path->agree/disagree mapping."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        cleaned = "\n".join(lines)

    try:
        mapping = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Failed to parse verify JSON: {exc}") from exc

    if not isinstance(mapping, dict):
        raise ValueError(f"Expected JSON object, got {type(mapping).__name__}")

    return mapping


def verify_curation(
    curated_files: list[CuratedFile],
    family: TaskFamily,
    repos: list[Path],
    *,
    sample_size: int = 5,
) -> str:
    """Verify curation quality by sampling files and asking LLM.

    Samples up to ``sample_size`` files from the curated set and up to
    ``sample_size`` from tracked files NOT in the set. Sends a single
    Haiku call to confirm/reject membership.

    Returns:
        "pass" if <=1 disagreement, "warn" if 2, "fail" if >2.
        Returns "pass" when LLM is unavailable (graceful degradation).
    """
    if not llm_available():
        logger.info("LLM unavailable, skipping curation verification (pass)")
        return "pass"

    # Gather curated paths
    curated_paths = [f.path for f in curated_files]
    curated_set = frozenset(curated_paths)

    # Sample from curated set
    in_sample = random.sample(curated_paths, min(sample_size, len(curated_paths)))

    # Gather all tracked files from repos, subtract curated set
    all_tracked: set[str] = set()
    for repo in repos:
        try:
            tracked = get_tracked_files(repo)
            all_tracked.update(tracked)
        except Exception as exc:
            logger.warning("Failed to get tracked files from %s: %s", repo, exc)

    not_curated = sorted(all_tracked - curated_set)
    not_sample = random.sample(not_curated, min(sample_size, len(not_curated)))

    if not in_sample and not not_sample:
        return "pass"

    prompt = _build_verify_prompt(in_sample, not_sample, family)

    try:
        response = call_claude(
            LLMRequest(prompt=prompt, model="haiku", timeout_seconds=30)
        )
        mapping = _parse_verify_response(response.text)
    except (LLMError, ValueError) as exc:
        logger.warning("LLM verification failed, returning pass (graceful): %s", exc)
        return "pass"

    # Count disagreements
    disagreements = sum(1 for v in mapping.values() if v.lower().strip() == "disagree")

    if disagreements <= 1:
        return "pass"
    if disagreements == 2:
        return "warn"
    return "fail"
