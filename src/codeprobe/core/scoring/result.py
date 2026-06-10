"""ScoreResult, verdict/materialization vocabularies, scorer-family registry,
and the Scorer protocol.

Split out of the original ``codeprobe/core/scoring.py`` module (pure
mechanical move — see the package ``__init__`` for the public surface).
"""

from __future__ import annotations

import inspect
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from codeprobe.analysis.stats import PASS_THRESHOLD  # noqa: F401  — re-exported

def read_task_metadata(task_dir: Path) -> dict:
    """Parse ``task_dir/metadata.json`` into a dict.

    Returns an empty dict on any failure (missing file, invalid JSON,
    unreadable). Callers apply their own defaults on missing keys.
    Single source of truth for metadata parsing — used by both the
    executor and DualScorer so the error handling stays consistent.
    """
    meta_path = task_dir / "metadata.json"
    if not meta_path.is_file():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def read_task_verification(task_dir: Path) -> dict:
    """Return the ``verification`` block from ``task_dir/metadata.json``."""
    verification = read_task_metadata(task_dir).get("verification") or {}
    return verification if isinstance(verification, dict) else {}


#: Canonical set of values for ``ScoreResult.verdict``. Splits the legacy
#: pass/fail boolean into four cases so a verifier-infrastructure failure
#: (e.g. ``git apply`` rejected the agent's diff because of a binary hunk)
#: is reported distinctly from an agent failure (test.sh exited non-zero).
#: The premortem for hybrid-execution-v1 identified silently collapsing
#: ``verifier_error`` into ``incorrect`` as the single most damaging
#: failure mode for hosted-agent comparisons.
ALLOWED_VERDICTS: frozenset[str] = frozenset(
    {
        "correct",
        "incorrect",
        "verifier_error",
        "inconclusive",
    }
)

#: Canonical set of values for ``ScoreResult.materialized_via``. Records
#: HOW the verifier got at the agent's final state. ``in_place`` is the
#: legacy worktree-mutation behavior; ``git_apply`` is the harness-
#: controlled clean-checkout path that lands in Slice 1b. ``file_overlay``
#: is reserved for vendor adapters (Devin, Codex Cloud) that return raw
#: file blobs rather than a unified diff.
ALLOWED_MATERIALIZATION: frozenset[str] = frozenset(
    {
        "in_place",
        "git_apply",
        "file_overlay",
    }
)


@dataclass(frozen=True)
class ScoreResult:
    """Result of scoring a task's agent output.

    ``score`` is the headline reward — the number that drives ranking,
    pass/fail, and ``mean_automated_score`` in aggregate.json. The exact
    rubric used to compute it is declared in ``scorer_family`` so reviewers
    can interpret the number. Per-task families let symbol-reference-trace
    use F1 (penalises both over-shipping and missing) while file-discovery
    triage tasks can opt into a recall-tilted family where over-shipping is
    free.

    ``reward`` mirrors ``score`` and exposes the unified ScoreResult
    contract field name. The two are always equal — ``__post_init__``
    back-fills ``reward`` from ``score`` and raises if a caller passes a
    contradictory value, so readers can trust either field. Keep
    ``score`` as the legacy field for the executor / aggregate consumers
    that read it directly, and read ``reward`` when working against the
    multi-rig contract (codeprobe / EB / CSB).

    ``scorer_family`` is the registered rubric name. See
    :data:`SCORER_FAMILIES` for the canonical set. Empty string means the
    scorer didn't declare one (treat as opaque).

    ``sub_scores`` exposes the rubric breakdown that produced the reward —
    e.g. ``{"recall": 0.94, "precision": 0.24, "f1": 0.38}`` for an IR
    family, or ``{"exit_code": 0}`` for binary. Callers MUST NOT use
    ``sub_scores`` to derive a different headline number; treat it as
    diagnostics.

    ``diagnostics`` is a free-form bag of run-time observations that don't
    affect reward but are useful for debugging. The unified contract
    surfaces ``ir_metrics`` here (mirror of the IR breakdown for callers
    that want a single canonical IR view); the executor injects
    ``task_time_seconds``, ``token_cost_usd``, ``input_tokens``, and
    ``output_tokens`` into the serialised ``scoring_details.diagnostics``
    block at scoring.json write time so the per-task contract is
    self-contained without forcing the scorer to know about run-level
    metadata. ``input_tokens`` / ``output_tokens`` are raw counts (sum
    across the multi-turn conversation for the trial); cost-Pareto plots
    that aren't dollar-locked need them to compare across models with
    different per-token pricing.

    ``ir_metrics`` is kept at the top level for back-compat with existing
    aggregate consumers (``cli/experiment_cmd.py`` reads
    ``scoring_details["recall"]`` / ``"precision"`` / ``"f1"`` directly).
    It mirrors ``diagnostics["ir_metrics"]``.

    ``reward_score`` mirrors ``score`` and is preserved so older callers
    that distinguish "human-shown score" from "ranking number" keep
    working. The two are equal by definition and ``__post_init__``
    enforces it; passing the kwarg at construction is redundant.

    ``details`` continues to carry the precision/recall/f1 fields for
    backward compatibility with aggregate.json consumers that read
    ``scoring_details["f1"]`` directly. Treat ``sub_scores`` /
    ``ir_metrics`` as the canonical source going forward.

    ``passed`` is preserved for back-compat (``score_passed`` in
    ``analysis/stats.py`` still consults it). For continuous rewards
    callers should compare ``score`` to a context-specific threshold
    rather than read this flag.
    """

    score: float
    passed: bool
    error: str | None = None
    details: dict = field(default_factory=dict)
    reward_score: float | None = None
    ir_metrics: dict = field(default_factory=dict)
    scorer_family: str = ""
    sub_scores: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    reward: float | None = None
    # ``verdict`` distinguishes agent-failure (incorrect) from verifier-
    # infrastructure failure (verifier_error) so a `git apply` rejection or a
    # missing test fixture doesn't get silently graded as an agent failure.
    # ``None`` means a caller hasn't migrated yet (legacy path); new call
    # sites MUST populate it. See ALLOWED_VERDICTS for the canonical set.
    verdict: str | None = None
    # ``materialized_via`` records HOW the verifier got at the agent's final
    # state. ``in_place`` is the legacy worktree-mutation behavior; new code
    # paths set ``git_apply`` (diff applied onto a clean checkout) or
    # ``file_overlay`` (vendor-returned files written into a clean tree).
    # See ALLOWED_MATERIALIZATION for the canonical set.
    materialized_via: str = "in_place"

    def __post_init__(self) -> None:
        # ``reward`` and ``reward_score`` mirror ``score`` — back-fill
        # when omitted, reject contradictions so there is exactly one
        # headline number and every reader can trust whichever alias it
        # consumes. Frozen dataclass requires ``object.__setattr__`` to
        # populate the defaults after init.
        for alias in ("reward", "reward_score"):
            value = getattr(self, alias)
            if value is None:
                object.__setattr__(self, alias, self.score)
            elif value != self.score:
                raise ValueError(
                    f"{alias}={value!r} contradicts score={self.score!r} — "
                    "the fields are aliases by contract; pass score only"
                )
        if self.verdict is not None and self.verdict not in ALLOWED_VERDICTS:
            raise ValueError(
                f"verdict={self.verdict!r} not in ALLOWED_VERDICTS "
                f"({sorted(ALLOWED_VERDICTS)})"
            )
        if self.materialized_via not in ALLOWED_MATERIALIZATION:
            raise ValueError(
                f"materialized_via={self.materialized_via!r} not in "
                f"ALLOWED_MATERIALIZATION ({sorted(ALLOWED_MATERIALIZATION)})"
            )


# ---------------------------------------------------------------------------
# Scorer family registry — names + rubric signature
# ---------------------------------------------------------------------------
#
# Each family is a label declaring how the reward was computed. The family
# is recorded on every ScoreResult so downstream tooling can interpret the
# number (e.g. "this F1 came from oracle_overlap_f1, not oracle_overlap_recall").
# Routing for IR-style tasks reads ``verification.scorer_family`` from
# task metadata; non-IR scorers (binary, continuous-without-metrics,
# exact_match, etc.) declare a fixed family at the scorer class.
#
# Adding a new family: update SCORER_FAMILIES, register a sub_scores shape
# in docs/scoring_model.md, and add a fixture-backed test in
# tests/test_scoring_reward.py.

SCORER_FAMILIES: frozenset[str] = frozenset(
    {
        # IR-style — oracle is a set of expected files / symbols
        "oracle_overlap_f1",  # symbol-reference-trace, file-list-tight (default)
        "oracle_overlap_fbeta",  # F-beta with per-task beta from verification.fbeta_beta
        "oracle_overlap_recall",  # file-discovery / triage (opt-in)
        "oracle_weighted_f1",  # org-scale tier-weighted oracle
        "oracle_weighted_recall",  # tier-weighted oracle, recall-tilted
        # Sequence-style — order matters
        "sequence_lcs",  # dependency_chain
        # Equality / scalar
        "exact_match",  # count, boolean, text
        # Test-script style — verifier emits reward directly
        "binary_test",  # test.sh exit code
        "continuous",  # reward.txt or stdout float, no IR
        # Composite
        "weighted_checkpoints",  # CheckpointScorer
        "oracle_checks",  # OracleChecksScorer — structured-rubric criteria
        "dual_composite",  # DualScorer (direct + artifact)
    }
)

# Default IR family when a task does not declare one. F1 is the
# conservative choice — symbol-reference-trace style tasks where shipping
# every file in the repo is "didn't solve" rather than "found everything".
# Tasks where dump-and-filter is fine (file-discovery / triage) opt into
# ``oracle_overlap_recall`` via ``verification.scorer_family``.
DEFAULT_IR_FAMILY: str = "oracle_overlap_f1"

# Default F-beta value when a task selects ``oracle_overlap_fbeta`` but
# doesn't pin ``verification.fbeta_beta``. ``beta=1.0`` makes the family
# numerically identical to F1; tasks that want over-ship penalised harder
# (symbol-reference-trace) configure beta < 1.0 (e.g. 0.5 weights
# precision twice as heavily as recall).
DEFAULT_FBETA_BETA: float = 1.0


def _read_fbeta_beta(task_dir: Path | None) -> float:
    """Resolve the per-task ``beta`` parameter for ``oracle_overlap_fbeta``.

    Reads ``verification.fbeta_beta`` from ``task_dir/metadata.json``.
    Falls back to :data:`DEFAULT_FBETA_BETA` (=1.0, F1-equivalent) when:
      * ``task_dir`` is None
      * the metadata field is absent / non-numeric / non-finite
      * the value is non-positive (F-beta is undefined for ``beta <= 0``)

    The fallback is silent — F1-equivalent behaviour is the documented
    default and surfacing a finding here would force tests that don't care
    about beta to set the field.
    """
    if task_dir is None:
        return DEFAULT_FBETA_BETA
    verification = read_task_verification(task_dir)
    raw = verification.get("fbeta_beta")
    if raw is None:
        return DEFAULT_FBETA_BETA
    try:
        beta = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_FBETA_BETA
    if not math.isfinite(beta) or beta <= 0.0:
        return DEFAULT_FBETA_BETA
    return beta


def _select_ir_family(task_dir: Path | None, *, weighted: bool = False) -> str:
    """Resolve the scorer family for an IR scorer.

    Routing order:
      1. ``verification.scorer_family`` in ``task_dir/metadata.json`` —
         explicit override always wins.
      2. ``oracle_weighted_f1`` when the oracle reports tier weights
         (caller passes ``weighted=True``).
      3. ``DEFAULT_IR_FAMILY`` otherwise.

    Unknown family strings are passed through as-is — the registry is
    advisory, not enforced. New per-task rubrics that haven't been added
    to ``SCORER_FAMILIES`` yet still flow through, so users aren't blocked
    by registry churn.
    """
    if task_dir is not None:
        verification = read_task_verification(task_dir)
        explicit = verification.get("scorer_family")
        if isinstance(explicit, str) and explicit:
            return explicit
    if weighted:
        return "oracle_weighted_f1"
    return DEFAULT_IR_FAMILY


# ---------------------------------------------------------------------------
# Scorer protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class Scorer(Protocol):
    """Protocol for scoring agent output against a task.

    Implementations must accept the agent's raw output and the task directory,
    returning a ScoreResult with a normalised score in [0.0, 1.0].

    Scorers that support the diff-materialisation path (Slice 1b) opt in
    by accepting an additional keyword-only ``agent_state: AgentState |
    None = None`` parameter. The executor detects support structurally
    via :func:`scorer_accepts_agent_state`; scorers without the kwarg
    keep the legacy in-place behavior.
    """

    def score(self, agent_output: str, task_dir: Path) -> ScoreResult: ...


def scorer_accepts_agent_state(scorer: object) -> bool:
    """Return True if ``scorer.score`` accepts an ``agent_state`` kwarg.

    Structural capability check so the executor never has to know
    concrete scorer types — any scorer (including third-party
    entry-point scorers) can opt into diff materialisation by accepting
    the parameter.
    """
    score = getattr(scorer, "score", None)
    if score is None:
        return False
    try:
        sig = inspect.signature(score)
    except (TypeError, ValueError):
        return False
    return "agent_state" in sig.parameters
