"""Multi-source consensus mining for MCP-advantaged ground truth.

When mining symbol-reference-trace / change-scope-audit tasks, run several
backends (sourcegraph, ast, grep) against the same symbol and only ship a
task when at least two backends agree above an F1 threshold. Tasks below
the threshold are quarantined with a divergence report so a human can
inspect why the backends disagree before the task ever enters an eval.

This is the structural fix for the ``--mcp-families`` tautology
(codeprobe-ekhi): if SG and AST and grep all converge on the same answer,
the ground truth is well-defined regardless of which tool the agent later
uses through MCP. If they diverge, the question is tool-dependent and the
task should be flagged or dropped.

ZFC compliance: pure mechanism. Each backend is a deterministic resolver
(parser walk, find_references RPC, byte-match), and the consensus decision
is a pairwise F1 calculation followed by an intersection or union — no
semantic judgement, no hidden thresholds. ``threshold`` is exposed on the
CLI so reviewers see the calibration knob explicitly.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from itertools import combinations
from pathlib import Path
from typing import Literal

from codeprobe.mining.cross_validate import compute_pair_metrics

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Backend identifiers
# ---------------------------------------------------------------------------


BackendName = Literal["sourcegraph", "ast", "grep"]
ConsensusMode = Literal["intersection", "union"]

DEFAULT_BACKENDS: tuple[BackendName, ...] = ("sourcegraph", "ast", "grep")
DEFAULT_THRESHOLD: float = 0.8
DEFAULT_MODE: ConsensusMode = "intersection"


# ---------------------------------------------------------------------------
# Backend resolvers — thin wrappers around the existing oracle backends so
# the consensus module owns its own protocol surface and we don't fan-out
# kwargs through the rest of the mining pipeline.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BackendResult:
    """Outcome of running one backend for one symbol.

    ``available`` is False when the backend could not run at all (missing
    auth, missing toolchain, repo not on disk). The caller skips it from
    the pairwise comparison rather than treating it as an empty set.
    ``error`` carries a short reason for the divergence report.
    """

    backend: BackendName
    files: frozenset[str] = frozenset()
    available: bool = True
    error: str | None = None


def _run_grep_backend(
    symbol: str, repo_paths: Sequence[Path]
) -> BackendResult:
    """Mechanical byte-match across the primary repo via :class:`RipgrepResolver`."""
    try:
        from codeprobe.mining.multi_repo import RipgrepResolver

        resolver = RipgrepResolver()
        refs = resolver.find_references(symbol, [str(p) for p in repo_paths])
    except Exception as exc:  # pragma: no cover - defensive
        return BackendResult(
            backend="grep", available=False, error=f"{type(exc).__name__}: {exc}"
        )
    files = frozenset(ref.path for ref in refs)
    return BackendResult(backend="grep", files=files)


def _run_ast_backend(
    symbol: str,
    repo_paths: Sequence[Path],
    *,
    defining_file: str = "",
) -> BackendResult:
    """Real parser walk via :class:`AstResolver` (Python + Go)."""
    try:
        from codeprobe.mining.ast_resolver import AstResolver

        resolver = AstResolver(defining_file=defining_file)
        refs = resolver.find_references(symbol, [str(p) for p in repo_paths])
    except Exception as exc:  # pragma: no cover - defensive
        return BackendResult(
            backend="ast", available=False, error=f"{type(exc).__name__}: {exc}"
        )
    files = frozenset(ref.path for ref in refs)
    return BackendResult(backend="ast", files=files)


def _run_sourcegraph_backend(
    symbol: str,
    repo_paths: Sequence[Path],
    *,
    defining_file: str,
    sg_repo: str,
    sg_url: str = "https://demo.sourcegraph.com",
) -> BackendResult:
    """Sourcegraph ``find_references`` via the existing helper.

    Skipped (returns ``available=False``) when ``sg_repo`` is empty or the
    user lacks Sourcegraph auth. The caller handles availability explicitly
    so callers see why a backend was excluded from the consensus.
    """
    if not sg_repo:
        return BackendResult(
            backend="sourcegraph",
            available=False,
            error="sg_repo not configured",
        )
    try:
        from codeprobe.mining.sg_auth import AuthError, get_valid_token
        from codeprobe.mining.sg_ground_truth import _call_find_references

        try:
            get_valid_token()
        except AuthError as exc:
            return BackendResult(
                backend="sourcegraph",
                available=False,
                error=f"AuthError: {exc}",
            )

        sg_files = _call_find_references(
            symbol=symbol,
            defining_file=defining_file,
            repo_sg_name=sg_repo,
            sg_url=sg_url,
        )
    except Exception as exc:  # pragma: no cover - defensive
        return BackendResult(
            backend="sourcegraph",
            available=False,
            error=f"{type(exc).__name__}: {exc}",
        )

    if sg_files is None:
        return BackendResult(
            backend="sourcegraph",
            available=False,
            error="find_references returned None (API failure)",
        )
    return BackendResult(backend="sourcegraph", files=frozenset(sg_files))


def _run_backend(
    name: BackendName,
    *,
    symbol: str,
    repo_paths: Sequence[Path],
    defining_file: str,
    sg_repo: str,
    sg_url: str,
) -> BackendResult:
    """Dispatch one backend by name."""
    if name == "grep":
        return _run_grep_backend(symbol, repo_paths)
    if name == "ast":
        return _run_ast_backend(
            symbol, repo_paths, defining_file=defining_file
        )
    if name == "sourcegraph":
        return _run_sourcegraph_backend(
            symbol,
            repo_paths,
            defining_file=defining_file,
            sg_repo=sg_repo,
            sg_url=sg_url,
        )
    return BackendResult(
        backend=name, available=False, error=f"unknown backend {name!r}"
    )


# ---------------------------------------------------------------------------
# Consensus decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConsensusDecision:
    """Outcome of running every requested backend for one task candidate.

    ``shipped`` is True when at least two available backends agree above
    ``threshold`` (pairwise F1). ``consensus_files`` is the intersection
    (default) or union of the file sets returned by available backends —
    callers use it as the task's ground truth when ``shipped``.

    ``divergence_report`` is a self-describing dict ready to be written to
    disk for shipped tasks (alongside ``ground_truth.json``) AND for
    quarantined tasks (the only artifact in ``tasks_quarantined/``).
    """

    shipped: bool
    consensus_files: frozenset[str]
    mode: ConsensusMode
    threshold: float
    min_pair_f1: float
    max_pair_f1: float
    available_backends: tuple[BackendName, ...]
    backends_attempted: tuple[BackendName, ...]
    backend_results: tuple[BackendResult, ...]
    divergence_report: dict = field(default_factory=dict)


def _build_divergence_report(
    *,
    symbol: str,
    defining_file: str,
    backend_results: Sequence[BackendResult],
    threshold: float,
    mode: ConsensusMode,
    pair_metrics: list[dict],
    decision: str,
    consensus_files: Iterable[str],
) -> dict:
    """Assemble the divergence_report payload.

    Schema is stable: callers (the writer, the CLI summary, tests) read
    these keys directly. Any new field must be additive.
    """
    return {
        "schema_version": "consensus.v1",
        "symbol": symbol,
        "defining_file": defining_file,
        "threshold": threshold,
        "mode": mode,
        "decision": decision,  # "shipped" or "quarantined"
        "backend_results": [
            {
                "backend": br.backend,
                "available": br.available,
                "n_files": len(br.files),
                "files": sorted(br.files),
                "error": br.error,
            }
            for br in backend_results
        ],
        "pair_metrics": pair_metrics,
        "consensus_files": sorted(consensus_files),
    }


def _pairwise_metrics(
    available: Sequence[BackendResult],
) -> tuple[list[dict], float, float]:
    """Compute symmetric F1 between every pair of available backends.

    Returns ``(pair_metrics, min_f1, max_f1)``. When fewer than two backends
    are available the F1 bounds default to ``0.0`` (treated as full
    disagreement) so a single-backend candidate cannot ship under
    consensus mode.
    """
    pair_metrics: list[dict] = []
    f1s: list[float] = []
    for ba, bb in combinations(available, 2):
        metrics = compute_pair_metrics(ba.files, bb.files)
        pair_metrics.append(
            {
                "backend_a": ba.backend,
                "backend_b": bb.backend,
                **metrics,
                f"{ba.backend}_only": sorted(ba.files - bb.files),
                f"{bb.backend}_only": sorted(bb.files - ba.files),
            }
        )
        f1s.append(float(metrics["f1"]))

    if not f1s:
        return pair_metrics, 0.0, 0.0
    return pair_metrics, min(f1s), max(f1s)


def _combine_files(
    available: Sequence[BackendResult], mode: ConsensusMode
) -> frozenset[str]:
    """Combine file sets across available backends.

    intersection (default): files reported by *every* available backend —
        high-precision oracle, conservative answer key.
    union: files reported by *any* available backend — high-recall, useful
        when the task framing tolerates over-inclusion.

    With a single available backend, both modes degenerate to that
    backend's file set, but :func:`compute_consensus` will still mark the
    candidate as quarantined because the agreement check requires at
    least two backends.
    """
    if not available:
        return frozenset()
    if mode == "union":
        out: frozenset[str] = frozenset()
        for br in available:
            out = out | br.files
        return out
    # intersection
    iterator = iter(available)
    out = next(iterator).files
    for br in iterator:
        out = out & br.files
    return out


def compute_consensus(
    *,
    symbol: str,
    defining_file: str,
    repo_paths: Sequence[Path],
    backends: Sequence[BackendName] = DEFAULT_BACKENDS,
    threshold: float = DEFAULT_THRESHOLD,
    mode: ConsensusMode = DEFAULT_MODE,
    sg_repo: str = "",
    sg_url: str = "https://demo.sourcegraph.com",
    max_workers: int = 3,
) -> ConsensusDecision:
    """Run *backends* for *symbol* and decide whether to ship.

    Parameters
    ----------
    symbol:
        The symbol whose references we want to resolve.
    defining_file:
        Repo-relative path of the symbol's defining file. Required by SG
        and used by :class:`AstResolver` for intra-package scoping.
    repo_paths:
        Local paths for the candidate repos. The first path is treated as
        primary for backends that don't natively support multi-repo input.
    backends:
        Subset of :data:`DEFAULT_BACKENDS` to run. Order is preserved in
        the report; comparison itself is order-independent.
    threshold:
        Minimum pairwise F1 required between *any* pair of available
        backends for the task to ship. Tasks below the threshold are
        marked ``shipped=False`` so the caller can quarantine them.
    mode:
        ``"intersection"`` (default) → consensus_files is the intersection
        across available backends (high-precision); ``"union"`` → union
        (high-recall).
    sg_repo:
        Sourcegraph repo identifier; required for the ``sourcegraph``
        backend. Empty → SG backend is reported as unavailable.

    Returns
    -------
    ConsensusDecision
    """
    if not 0.0 <= threshold <= 1.0:
        raise ValueError(f"threshold must be in [0, 1], got {threshold!r}")
    if mode not in ("intersection", "union"):
        raise ValueError(
            f"mode must be 'intersection' or 'union', got {mode!r}"
        )
    if not backends:
        raise ValueError("backends must be non-empty")
    if not symbol:
        raise ValueError("symbol must be non-empty")

    backends_attempted = tuple(backends)

    # Run backends in parallel — they don't share state.
    results: dict[BackendName, BackendResult] = {}
    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        futures = {
            pool.submit(
                _run_backend,
                name,
                symbol=symbol,
                repo_paths=repo_paths,
                defining_file=defining_file,
                sg_repo=sg_repo,
                sg_url=sg_url,
            ): name
            for name in backends_attempted
        }
        for fut in as_completed(futures):
            res = fut.result()
            results[res.backend] = res

    # Preserve the original backend order in the report.
    ordered_results = tuple(
        results[name] for name in backends_attempted if name in results
    )
    available = tuple(br for br in ordered_results if br.available)
    available_names = tuple(br.backend for br in available)

    pair_metrics, min_f1, max_f1 = _pairwise_metrics(available)
    consensus_files = _combine_files(available, mode)

    # Ship when at least two backends ran AND any pair agrees above threshold.
    shipped = len(available) >= 2 and max_f1 >= threshold
    decision_label = "shipped" if shipped else "quarantined"

    divergence_report = _build_divergence_report(
        symbol=symbol,
        defining_file=defining_file,
        backend_results=ordered_results,
        threshold=threshold,
        mode=mode,
        pair_metrics=pair_metrics,
        decision=decision_label,
        consensus_files=consensus_files,
    )

    logger.info(
        "Consensus %s for %s: %d/%d backends available, "
        "max_pair_f1=%.3f (threshold=%.2f), n_consensus=%d",
        decision_label,
        symbol,
        len(available),
        len(ordered_results),
        max_f1,
        threshold,
        len(consensus_files),
    )

    return ConsensusDecision(
        shipped=shipped,
        consensus_files=consensus_files,
        mode=mode,
        threshold=threshold,
        min_pair_f1=min_f1,
        max_pair_f1=max_f1,
        available_backends=available_names,
        backends_attempted=backends_attempted,
        backend_results=ordered_results,
        divergence_report=divergence_report,
    )


# ---------------------------------------------------------------------------
# CLI helpers
# ---------------------------------------------------------------------------


def parse_backend_list(raw: str | None) -> tuple[BackendName, ...]:
    """Parse ``--consensus-backends`` value (comma-separated names).

    Empty / None returns :data:`DEFAULT_BACKENDS`. Unknown names raise
    :class:`ValueError`; callers may surface this as a Click error.
    """
    if not raw:
        return DEFAULT_BACKENDS

    valid = set(DEFAULT_BACKENDS)
    out: list[BackendName] = []
    seen: set[str] = set()
    for token in raw.split(","):
        name = token.strip().lower()
        if not name:
            continue
        if name in seen:
            continue
        if name not in valid:
            raise ValueError(
                f"unknown consensus backend {name!r} "
                f"(expected one of: {', '.join(sorted(valid))})"
            )
        seen.add(name)
        out.append(name)  # type: ignore[arg-type]
    if not out:
        return DEFAULT_BACKENDS
    return tuple(out)
