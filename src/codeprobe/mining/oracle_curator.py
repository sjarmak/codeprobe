"""Multi-backend oracle curator for symbol-reference-trace mining.

Builds a per-file curated ground-truth set from the raw output of multiple
search backends (grep, AST, Sourcegraph, ...). Tier-1 keeps any file that
at least ``min_backends`` distinct backends agreed on. Tier-2 (a single
backend) routes through an LLM curator that reads the actual code and
votes keep / reject — a structural mitigation for the single-tool oracle
bias surfaced by codeprobe-wo7n's gascity rerun.

The curator runs INSIDE the ``shipped`` branch of consensus mining: by
the time it sees ``BackendResult``s the F1 gate (in ``consensus.py``) has
already accepted the candidate; the curator's job is then to assemble a
fair per-file answer key and record provenance.

ZFC compliance — mechanism only:

- Tier-1 selection is arithmetic (count distinct backends per file).
- Tier-2 LLM curation delegates the keep/reject judgment to the model;
  the surrounding code does IO (read snippet) and structural validation
  (parse JSON, check ``keep`` is a bool).
- Single-backend fallback is a documented offline mode where no
  consensus filter is possible — every file is kept as ``required``.

The output is a ``CuratedOracle`` carrying per-file backend provenance
plus a per-task ``backends_consensus`` summary so downstream
bias-warning code can compare the agent's MCP surface against the set
of backends used to construct the answer key.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from codeprobe.core.llm import (
    LLMError,
    LLMRequest,
    call_claude,
    llm_available,
)
from codeprobe.mining.consensus import BackendResult, ConsensusDecision

logger = logging.getLogger(__name__)


DEFAULT_MIN_BACKENDS = 2

# Snippet reading bounds for the LLM curator prompt. Files larger than
# these caps are truncated; the model judges from a representative head
# of the file rather than the full contents.
_MAX_SNIPPET_LINES = 80
_MAX_SNIPPET_BYTES = 8000


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CuratorVote:
    """Outcome of one LLM curator call for a tier-2 candidate file.

    ``keep`` is the model's verdict; ``rationale`` is a one-sentence
    justification. ``error`` is set when the call failed (LLM
    unavailable, parse failure, timeout); in that case ``keep`` is
    forced to ``False`` so the conservative path drops the candidate.
    """

    keep: bool
    rationale: str = ""
    error: str | None = None


@dataclass(frozen=True)
class CuratedItem:
    """A single curated ground-truth file with per-file provenance.

    ``backends`` is the sorted tuple of backend names that found this
    file. ``tier`` is one of ``"required"`` | ``"supplementary"`` and
    drives the weighted F1 in the vendored oracle. ``via_llm_review``
    is True when the file passed the tier-2 LLM curator path; in that
    case ``llm_rationale`` carries the model's keep rationale.
    """

    path: str
    backends: tuple[str, ...]
    tier: str
    via_llm_review: bool
    llm_rationale: str = ""


@dataclass(frozen=True)
class CuratedOracle:
    """Curated ground truth for one task, returned by ``curate_consensus``.

    ``items`` is the sorted tuple of kept files. ``backends_consensus``
    is the sorted tuple of backend names that contributed at least one
    kept file — this is the value that ground_truth.json's
    ``oracle_backends_consensus`` field exposes for bias-warning logic.
    ``quarantined`` lists ``(path, reason)`` pairs the curator dropped
    so reviewers can audit the LLM curator decisions.
    """

    items: tuple[CuratedItem, ...]
    backends_consensus: tuple[str, ...]
    quarantined: tuple[tuple[str, str], ...]
    min_backends: int
    llm_used: bool


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def curate_consensus(
    *,
    backend_results: Sequence[BackendResult],
    symbol: str,
    defining_file: str,
    repo_paths: Sequence[Path],
    min_backends: int = DEFAULT_MIN_BACKENDS,
    use_llm: bool = True,
    llm_timeout_seconds: int = 30,
) -> CuratedOracle:
    """Curate per-file ground truth from N backend results.

    Tier-1: files reported by >= ``min_backends`` distinct backends are
    kept as ``"required"`` with no LLM call.

    Tier-2: files reported by exactly one backend are routed through an
    LLM curator that reads the candidate snippet and votes
    ``keep`` / ``reject``. Approved items are kept as
    ``"supplementary"``; rejected items are quarantined with the
    rationale.

    Single-backend fallback: when only one backend ran (``available
    == 1``), every file is kept as ``"required"`` because there is
    nothing to consensus-filter against. This preserves behavior in
    ripgrep-only environments and is documented in
    ``docs/oracle_curator.md``.

    LLM unavailability: when ``use_llm=False`` or
    :func:`llm_available` returns False, tier-2 candidates are
    quarantined (NOT dropped silently) so callers can see how many
    items were lost and decide whether to retry with an LLM available.
    """
    if min_backends < 1:
        raise ValueError(f"min_backends must be >= 1, got {min_backends!r}")

    available = [br for br in backend_results if br.available]
    if not available:
        logger.warning(
            "Oracle curator: no available backends — returning empty oracle"
        )
        return CuratedOracle(
            items=(),
            backends_consensus=(),
            quarantined=(),
            min_backends=min_backends,
            llm_used=False,
        )

    n_available = len(available)

    # Build per-path → set[backend_name] map.
    per_path: dict[str, set[str]] = {}
    for br in available:
        for path in br.files:
            per_path.setdefault(path, set()).add(br.backend)

    # Single-backend fallback: skip both tier filtering and LLM curation.
    if n_available == 1:
        only_backend = available[0].backend
        items = tuple(
            CuratedItem(
                path=path,
                backends=(only_backend,),
                tier="required",
                via_llm_review=False,
            )
            for path in sorted(per_path)
        )
        consensus = (only_backend,) if items else ()
        return CuratedOracle(
            items=items,
            backends_consensus=consensus,
            quarantined=(),
            min_backends=min_backends,
            llm_used=False,
        )

    items: list[CuratedItem] = []
    quarantined: list[tuple[str, str]] = []
    llm_called = False

    llm_ok = use_llm and llm_available()

    for path in sorted(per_path):
        backends = tuple(sorted(per_path[path]))
        n = len(backends)

        if n >= min_backends:
            items.append(
                CuratedItem(
                    path=path,
                    backends=backends,
                    tier="required",
                    via_llm_review=False,
                )
            )
            continue

        # Tier-2: single-backend disagreement.
        if not llm_ok:
            quarantined.append(
                (path, "single-backend, LLM unavailable")
            )
            continue

        llm_called = True
        vote = _curate_with_llm(
            symbol=symbol,
            defining_file=defining_file,
            candidate_path=path,
            found_by=backends[0],
            repo_paths=repo_paths,
            timeout_seconds=llm_timeout_seconds,
        )
        if vote.error:
            quarantined.append((path, f"LLM error: {vote.error}"))
        elif vote.keep:
            items.append(
                CuratedItem(
                    path=path,
                    backends=backends,
                    tier="supplementary",
                    via_llm_review=True,
                    llm_rationale=vote.rationale,
                )
            )
        else:
            quarantined.append(
                (path, f"LLM rejected: {vote.rationale}")
            )

    # backends_consensus = union of backends that contributed >=1 kept item.
    consensus_set: set[str] = set()
    for it in items:
        consensus_set.update(it.backends)

    return CuratedOracle(
        items=tuple(items),
        backends_consensus=tuple(sorted(consensus_set)),
        quarantined=tuple(quarantined),
        min_backends=min_backends,
        llm_used=llm_called,
    )


def curate_consensus_decision(
    decision: ConsensusDecision,
    *,
    symbol: str,
    defining_file: str,
    repo_paths: Sequence[Path],
    min_backends: int = DEFAULT_MIN_BACKENDS,
    use_llm: bool = True,
) -> CuratedOracle:
    """Convenience wrapper that curates from a :class:`ConsensusDecision`.

    Pulls ``backend_results`` off the decision and forwards every other
    parameter. Useful at call sites that already produced a
    ``ConsensusDecision`` via :func:`compute_consensus`.
    """
    return curate_consensus(
        backend_results=decision.backend_results,
        symbol=symbol,
        defining_file=defining_file,
        repo_paths=repo_paths,
        min_backends=min_backends,
        use_llm=use_llm,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_snippet(repo_paths: Sequence[Path], rel_path: str) -> str:
    """Read a bounded text snippet of *rel_path* from the first repo where
    it exists.

    Caps both line count (:data:`_MAX_SNIPPET_LINES`) and byte count
    (:data:`_MAX_SNIPPET_BYTES`) so the LLM prompt stays small and
    deterministic regardless of file size. Returns ``""`` when the file
    is not present in any of the supplied repos.
    """
    for rp in repo_paths:
        full = rp / rel_path
        try:
            if not full.is_file():
                continue
            data = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if len(data) > _MAX_SNIPPET_BYTES:
            data = data[:_MAX_SNIPPET_BYTES]
        lines = data.splitlines()
        if len(lines) > _MAX_SNIPPET_LINES:
            lines = lines[:_MAX_SNIPPET_LINES]
        return "\n".join(lines)
    return ""


def _curate_with_llm(
    *,
    symbol: str,
    defining_file: str,
    candidate_path: str,
    found_by: str,
    repo_paths: Sequence[Path],
    timeout_seconds: int,
) -> CuratorVote:
    """Ask an LLM whether *candidate_path* truly references *symbol*.

    The model receives the symbol name, defining file, candidate path,
    which backend found it, and a bounded snippet of the file. It must
    respond with a JSON object ``{"keep": bool, "rationale": str}``.
    Any deviation (non-JSON, missing field, wrong type) is treated as
    an error and the vote is forced to ``keep=False`` so quarantining
    is the conservative default.
    """
    snippet = _read_snippet(repo_paths, candidate_path)
    if not snippet:
        snippet = "(file not readable from any provided repo path)"

    prompt = (
        "You are an oracle curator for a code-search benchmark. A "
        "symbol-reference search ran multiple backends; this candidate "
        "file was reported by exactly one of them. Decide whether it "
        "actually references the symbol — directly, via alias, via "
        "re-export, or through wildcard import.\n\n"
        f"**Symbol:** {symbol}\n"
        f"**Defining file:** {defining_file}\n"
        f"**Candidate file:** {candidate_path}\n"
        f"**Found by backend:** {found_by}\n\n"
        "**Candidate snippet (truncated):**\n"
        "```\n"
        f"{snippet}\n"
        "```\n\n"
        "Respond with JSON only, exactly of the form:\n"
        '{"keep": true|false, "rationale": "<one short sentence>"}\n'
        "No markdown fences, no extra commentary."
    )

    try:
        response = call_claude(
            LLMRequest(
                prompt=prompt,
                model="haiku",
                timeout_seconds=timeout_seconds,
            )
        )
    except LLMError as exc:
        logger.warning(
            "Oracle curator LLM call failed for %s: %s",
            candidate_path,
            exc,
        )
        return CuratorVote(keep=False, error=str(exc))

    text = response.text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            line for line in lines if not line.strip().startswith("```")
        )

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        logger.warning(
            "Oracle curator: non-JSON response for %s",
            candidate_path,
        )
        return CuratorVote(keep=False, error="non-JSON response")

    if not isinstance(parsed, dict):
        return CuratorVote(
            keep=False, error="response not a JSON object"
        )

    keep = parsed.get("keep")
    if not isinstance(keep, bool):
        return CuratorVote(
            keep=False, error="missing or invalid 'keep' field"
        )

    rationale = parsed.get("rationale", "")
    if not isinstance(rationale, str):
        rationale = ""
    # Bound the rationale length so a chatty model can't blow up
    # ground_truth.json metadata.
    return CuratorVote(keep=keep, rationale=rationale[:500])
