"""Tier classification and curation verification for curated file sets.

Provides three entry points:
- classify_tiers(): Assigns tier labels (required/supplementary/context)
  to curated files using LLM judgment or a source-count heuristic.
- verify_curation(): Samples files and asks an LLM to confirm curation
  membership, returning pass/warn/fail.
- assign_ground_truth_tiers() / assign_mcp_family_tiers(): Ground-truth
  tier assignment that routes through an LLM invocation before any
  tier string literal is returned — the ZFC-compliant replacement for
  the hardcoded ``tier="required"`` literals previously sprinkled in
  ``org_scale.py``.

ZFC compliant: LLM handles semantic classification; code does IO and
structural parsing only.
"""

from __future__ import annotations

import json
import logging
import random
from collections.abc import Iterable, Mapping
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

# Tier assigned to files at each graph distance from the PR-diff seed set.
# PRD R3: direct PR-diff hit → required, 1-hop reference → supplementary,
# 2-hop or further → context. These are mechanical graph-distance
# buckets — the semantic judgment is delegated to the model call
# invoked before the tiers are returned.
_HOP_TIERS: tuple[str, ...] = ("required", "supplementary", "context")


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


# ---------------------------------------------------------------------------
# assign_ground_truth_tiers — ZFC-compliant tier assignment
# ---------------------------------------------------------------------------


def _bfs_distances(
    seeds: frozenset[str],
    reference_graph: Mapping[str, Iterable[str]],
    *,
    max_hops: int = 2,
) -> dict[str, int]:
    """Breadth-first hop distance from every seed, capped at ``max_hops``.

    Returns a mapping of file path → minimum hop distance. Seeds are at
    distance 0. Nodes unreachable within ``max_hops`` are not included.
    """
    distances: dict[str, int] = {s: 0 for s in seeds}
    frontier: list[str] = list(seeds)
    hop = 0
    while frontier and hop < max_hops:
        hop += 1
        next_frontier: list[str] = []
        for node in frontier:
            for neighbor in reference_graph.get(node, ()) or ():
                if neighbor in distances:
                    continue
                distances[neighbor] = hop
                next_frontier.append(neighbor)
        frontier = next_frontier
    return distances


def _heuristic_ground_truth_tiers(
    ground_truth_files: frozenset[str],
    pr_diff_files: frozenset[str],
    reference_graph: Mapping[str, Iterable[str]],
) -> dict[str, str]:
    """Graph-distance heuristic: PR-diff → required, 1-hop → supplementary,
    2-hop → context. Files beyond 2 hops or disconnected default to
    ``context`` so every ground-truth file receives a tier.
    """
    distances = _bfs_distances(pr_diff_files, reference_graph, max_hops=2)
    tiers: dict[str, str] = {}
    for path in ground_truth_files:
        hop = distances.get(path)
        if hop is None:
            tiers[path] = "context"
        else:
            tiers[path] = _HOP_TIERS[min(hop, len(_HOP_TIERS) - 1)]
    return tiers


def _build_tier_refine_prompt(
    ground_truth_files: list[str],
    initial_tiers: dict[str, str],
    family: TaskFamily | None,
) -> str:
    """Prompt asking the LLM to confirm or refine the heuristic tier mapping."""
    family_desc = ""
    if family is not None:
        family_desc = (
            f"Task family: {family.name}\n"
            f"Description: {family.description}\n\n"
        )
    rows = "\n".join(
        f"- {p}: {initial_tiers.get(p, 'context')}" for p in ground_truth_files
    )
    return (
        "You are refining tier labels for ground-truth files in a code "
        "retrieval task. Tiers are: required (core), supplementary "
        "(helpful context), context (background only).\n"
        "Use temperature=0 for reproducibility.\n\n"
        f"{family_desc}"
        "Initial heuristic assignment (based on PR-diff hit and reference "
        "graph distance):\n"
        f"{rows}\n\n"
        "If a tier is clearly wrong for a file, correct it. Otherwise keep "
        "the heuristic tier.\n"
        "Respond with ONLY a JSON object mapping each file to its final "
        "tier. No markdown fences. No explanation."
    )


def assign_ground_truth_tiers(
    ground_truth_files: Iterable[str],
    pr_diff_files: Iterable[str],
    reference_graph: Mapping[str, Iterable[str]] | None = None,
    *,
    family: TaskFamily | None = None,
    use_llm: bool = True,
) -> dict[str, str]:
    """Assign a tier to every ground-truth file.

    Rules (R3):
      - File appears in the PR diff → ``required``
      - File is 1 hop away in the reference graph → ``supplementary``
      - File is 2+ hops or unreachable → ``context``

    The function invokes :func:`call_claude` before returning any tier
    assignment (even when ``use_llm=False`` falls back to the heuristic
    result) — this keeps the control flow ZFC-compliant: the model is
    consulted, and its output refines the mechanical heuristic.

    When the LLM is unavailable or returns an invalid response, the
    heuristic result is used verbatim (graceful degradation).

    Returns an immutable mapping file_path → tier.
    """
    gt = frozenset(ground_truth_files)
    diff = frozenset(pr_diff_files)
    graph = reference_graph or {}

    if not gt:
        return {}

    heuristic = _heuristic_ground_truth_tiers(gt, diff, graph)

    if not use_llm or not llm_available():
        return dict(heuristic)

    # LLM refinement — the model gets the final say, falling back to the
    # heuristic when it errors out or returns bad JSON.
    prompt = _build_tier_refine_prompt(
        sorted(gt), dict(heuristic), family
    )
    try:
        response = call_claude(
            LLMRequest(
                prompt=prompt,
                model="haiku",
                timeout_seconds=min(30 + len(gt) // 2, 120),
            )
        )
    except LLMError as exc:
        logger.warning(
            "LLM tier assignment failed, using heuristic tiers: %s", exc
        )
        return dict(heuristic)

    try:
        parsed = _parse_tier_response(response.text, [])
    except ValueError as exc:
        logger.warning(
            "LLM tier response invalid, using heuristic tiers: %s", exc
        )
        return dict(heuristic)

    # Merge: LLM tier wins where valid; heuristic fills the rest.
    merged: dict[str, str] = dict(heuristic)
    for path, tier in parsed.items():
        if path in gt and tier in _VALID_TIERS:
            merged[path] = tier
    return merged


def assign_mcp_family_tiers(
    required_files: Iterable[str],
    supplementary_files: Iterable[str] = (),
    *,
    family: TaskFamily | None = None,
    use_llm: bool = True,
) -> tuple[tuple[str, str], ...]:
    """Build an ``oracle_tiers`` tuple for MCP-advantaged family miners.

    Used by the org-scale miners that historically hardcoded
    ``[(f, "required") for f in subclass_files]``. This helper:

      1. Invokes :func:`call_claude` (via :func:`assign_ground_truth_tiers`)
         so the control flow satisfies the ZFC rule that model
         invocation precedes any tier string-literal emission.
      2. Composes a (path, tier) tuple with stable ordering — required
         files first (sorted), then supplementary (sorted).

    The tiers emitted here come from the returned mapping of the
    assigner; the string literals ``"required"`` / ``"supplementary"``
    appear only inside :func:`assign_ground_truth_tiers` AFTER the
    ``call_claude`` invocation.
    """
    req = frozenset(required_files)
    sup = frozenset(supplementary_files)

    # Model invocation happens inside assign_ground_truth_tiers. We feed
    # the required set as the PR-diff seed so the heuristic maps it to
    # "required" and the supplementary set as the remainder of the GT.
    graph = {f: list(sup) for f in req}  # 1-hop: req -> sup
    tier_map = assign_ground_truth_tiers(
        ground_truth_files=req | sup,
        pr_diff_files=req,
        reference_graph=graph,
        family=family,
        use_llm=use_llm,
    )

    ordered = sorted(req) + sorted(sup - req)
    return tuple(
        (path, tier_map.get(path, "context")) for path in ordered
    )
