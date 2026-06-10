"""IR-style metric arithmetic and oracle answer-type scorers — precision /
recall / F-beta, path and symbol normalization, LCS, and the module-level
``score_*`` functions used by the oracle answer-type registry.

Split out of the original ``codeprobe/core/scoring.py`` module (pure
mechanical move — see the package ``__init__`` for the public surface).
"""

from __future__ import annotations

import math

from codeprobe.analysis.stats import PASS_THRESHOLD
from codeprobe.core.scoring.result import (
    DEFAULT_FBETA_BETA,
    DEFAULT_IR_FAMILY,
    ScoreResult,
)

# Named constant for zero-score returns — ensures every zero path is
# either (a) legitimate arithmetic (F1 with empty sets) or (b) paired
# with an explicit logger.warning (R16: fail-loud, no silent fallbacks).
_ZERO_SCORE: float = 0.0


def _normalize_path(p: str) -> str:
    """Normalize a file path for comparison — strip prefixes and separators."""
    p = p.replace("\\", "/").strip()
    for pfx in ("./", "/workspace/", "/tmp/", "/app/"):
        while p.startswith(pfx):
            p = p[len(pfx) :]
    return p.lstrip("/")


# ---------------------------------------------------------------------------
# Oracle answer_type scoring functions (module-level for registry use)
# ---------------------------------------------------------------------------


def _ir_metrics(
    expected_set: frozenset[str], actual_set: frozenset[str]
) -> tuple[float, float, float]:
    """Return ``(precision, recall, f1)`` for two normalized sets.

    Empty inputs collapse to zero — same convention as ``_compute_f1``.
    Used by the IR scorers to populate ``ScoreResult.ir_metrics`` next to
    the reward. Pure arithmetic, no judgment.
    """
    if not expected_set or not actual_set:
        return 0.0, 0.0, 0.0
    intersection = len(expected_set & actual_set)
    precision = intersection / len(actual_set)
    recall = intersection / len(expected_set)
    if precision + recall == 0:
        return precision, recall, 0.0
    f1 = 2 * precision * recall / (precision + recall)
    return precision, recall, f1


def _compute_f1(expected: list[str], actual: list[str]) -> float:
    """Compute F1 score from two lists of file paths.

    Zero returns here are legitimate arithmetic (empty sets, no overlap),
    not silent error fallbacks — they use ``_ZERO_SCORE`` to make the
    distinction explicit and to keep the regex in criteria.toml#R16 honest.
    """
    expected_set = frozenset(_normalize_path(p) for p in expected if p)
    actual_set = frozenset(_normalize_path(p) for p in actual if p)
    if not expected_set:
        return _ZERO_SCORE
    if not actual_set:
        return _ZERO_SCORE
    _, _, f1 = _ir_metrics(expected_set, actual_set)
    return f1 if f1 > 0.0 else _ZERO_SCORE


def score_file_list(
    expected: object,
    actual: object,
    *,
    family: str = DEFAULT_IR_FAMILY,
    beta: float = DEFAULT_FBETA_BETA,
) -> ScoreResult:
    """Score a file_list answer_type under the given scorer ``family``.

    Default family is ``oracle_overlap_f1`` — F1 penalises both over-
    shipping (low precision) and under-shipping (low recall). Tasks where
    dump-and-filter is fine (file-discovery / triage) opt into
    ``oracle_overlap_recall`` and reward becomes pure recall.

    The family routing usually happens upstream in
    :class:`ArtifactScorer` from ``verification.scorer_family``; passing
    ``family`` directly is supported for tests and for callers that score
    bare lists outside the artifact-scorer flow. ``beta`` only matters
    when ``family == "oracle_overlap_fbeta"``; defaults to 1.0 (≡ F1).
    """
    if not isinstance(expected, list):
        return ScoreResult(
            score=0.0,
            passed=False,
            error=f"file_list expected answer must be a list, got {type(expected).__name__}",
            scorer_family=family,
        )
    if not isinstance(actual, list):
        return ScoreResult(
            score=0.0,
            passed=False,
            error=f"file_list actual answer must be a list, got {type(actual).__name__}",
            scorer_family=family,
        )
    expected_set = frozenset(_normalize_path(p) for p in expected if p)
    actual_set = frozenset(_normalize_path(p) for p in actual if p)
    precision, recall, f1 = _ir_metrics(expected_set, actual_set)
    ir_metrics = {"precision": precision, "recall": recall, "f1": f1}
    reward = _ir_reward_from_family(
        family, precision=precision, recall=recall, f1=f1, beta=beta
    )
    sub_scores = {**ir_metrics, "reward": reward}
    if family == "oracle_overlap_fbeta":
        sub_scores["fbeta_beta"] = beta
    return ScoreResult(
        score=reward,
        passed=reward >= PASS_THRESHOLD,
        details=dict(ir_metrics),
        reward_score=reward,
        ir_metrics=ir_metrics,
        scorer_family=family,
        sub_scores=sub_scores,
        diagnostics={"ir_metrics": dict(ir_metrics)},
    )


def _fbeta(precision: float, recall: float, beta: float) -> float:
    """Compute the F-beta score from precision and recall.

    F-beta = (1 + β²) · P · R / (β² · P + R), with the standard convention
    that an empty intersection (precision = recall = 0) returns 0. Same
    convention as :func:`_ir_metrics`.
    """
    if beta <= 0.0 or not math.isfinite(beta):
        beta = DEFAULT_FBETA_BETA
    beta_sq = beta * beta
    denom = beta_sq * precision + recall
    if denom <= 0.0:
        return 0.0
    return (1.0 + beta_sq) * precision * recall / denom


def _ir_reward_from_family(
    family: str,
    *,
    precision: float,
    recall: float,
    f1: float,
    beta: float = DEFAULT_FBETA_BETA,
    weighted_recall: float | None = None,
) -> float:
    """Derive an IR reward from precision/recall/f1 under ``family``.

    The single family→reward formula table: :func:`score_file_list`,
    :func:`score_symbol_list`, the legacy file-list scorer, and
    ``ContinuousScorer._derive_reward_and_metrics`` all route through it
    so the same family name can never yield different formulas depending
    on which code path scored the task. Falls back to F1 for any
    unrecognised family — the conservative choice since over-shipping
    should generally cost something.

    ``beta`` is consumed only by the ``oracle_overlap_fbeta`` family. The
    default (1.0) makes that family numerically equivalent to F1; callers
    that want a different bias supply the value resolved from per-task
    metadata via :func:`_read_fbeta_beta`.

    ``weighted_recall`` is consumed only by ``oracle_weighted_recall``.
    IR list scorers don't see tier weights and omit it; the tier-weighted
    value surfaces via ContinuousScorer over the on-disk oracle's
    ``metrics.json``. Recall→recall is an honest same-axis substitution
    when the weighted value is absent.
    """
    if family == "oracle_overlap_recall":
        return recall
    if family == "oracle_weighted_recall":
        return weighted_recall if weighted_recall is not None else recall
    if family == "oracle_overlap_fbeta":
        return _fbeta(precision, recall, beta)
    # F1 families: oracle_overlap_f1 (default), oracle_weighted_f1 (the
    # on-disk weighted oracle stores weighted_f1 in the ``f1`` field —
    # see mining/writer.py:_ORACLE_PY), and unrecognised families.
    return f1


def score_count(expected: object, actual: object) -> ScoreResult:
    """Exact integer match."""
    try:
        passed = int(expected) == int(actual)  # type: ignore[call-overload]
    except (ValueError, TypeError):
        return ScoreResult(
            score=0.0,
            passed=False,
            error="count values must be convertible to int",
            scorer_family="exact_match",
        )
    score_val = 1.0 if passed else 0.0
    return ScoreResult(
        score=score_val,
        passed=passed,
        scorer_family="exact_match",
        sub_scores={"match": score_val},
    )


def score_exact_match(expected: object, actual: object) -> ScoreResult:
    """Normalised exact match (strip + lowercase). Used for boolean and text."""
    passed = str(expected).strip().lower() == str(actual).strip().lower()
    score_val = 1.0 if passed else 0.0
    return ScoreResult(
        score=score_val,
        passed=passed,
        scorer_family="exact_match",
        sub_scores={"match": score_val},
    )


def _normalize_symbol(s: str) -> str:
    """Normalize a symbol name for comparison.

    Strips module prefixes (split on '.' and '::'), lowercases, and strips
    whitespace.  E.g. ``"foo.bar.MyClass"`` -> ``"myclass"``.
    """
    # Split on '::' first, take last segment, then split on '.', take last
    s = s.split("::")[-1].split(".")[-1]
    return s.strip().lower()


def score_symbol_list(
    expected: object,
    actual: object,
    *,
    family: str = DEFAULT_IR_FAMILY,
    beta: float = DEFAULT_FBETA_BETA,
) -> ScoreResult:
    """Score a symbol_list answer_type under the given scorer ``family``.

    Same family routing as :func:`score_file_list` — see that docstring
    for rationale. The default is ``oracle_overlap_f1``. ``beta`` only
    matters under ``oracle_overlap_fbeta``.
    """
    exp = expected if isinstance(expected, list) else []
    act = actual if isinstance(actual, list) else []
    exp_set = frozenset(_normalize_symbol(str(s)) for s in exp if s)
    act_set = frozenset(_normalize_symbol(str(s)) for s in act if s)
    if not exp_set or not act_set:
        empty = {"precision": 0.0, "recall": 0.0, "f1": 0.0}
        sub_scores: dict = {**empty, "reward": 0.0}
        if family == "oracle_overlap_fbeta":
            sub_scores["fbeta_beta"] = beta
        return ScoreResult(
            score=0.0,
            passed=False,
            details=dict(empty),
            reward_score=0.0,
            ir_metrics=empty,
            scorer_family=family,
            sub_scores=sub_scores,
            diagnostics={"ir_metrics": dict(empty)},
        )
    precision, recall, f1 = _ir_metrics(exp_set, act_set)
    ir_metrics = {"precision": precision, "recall": recall, "f1": f1}
    reward = _ir_reward_from_family(
        family, precision=precision, recall=recall, f1=f1, beta=beta
    )
    sub_scores = {**ir_metrics, "reward": reward}
    if family == "oracle_overlap_fbeta":
        sub_scores["fbeta_beta"] = beta
    return ScoreResult(
        score=reward,
        passed=reward >= PASS_THRESHOLD,
        details=dict(ir_metrics),
        reward_score=reward,
        ir_metrics=ir_metrics,
        scorer_family=family,
        sub_scores=sub_scores,
        diagnostics={"ir_metrics": dict(ir_metrics)},
    )


def _lcs_length(a: list[str], b: list[str]) -> int:
    """Compute the length of the longest common subsequence (DP)."""
    m, n = len(a), len(b)
    if m == 0 or n == 0:
        return 0
    # Use 1D DP array for space efficiency
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        curr = [0] * (n + 1)
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev = curr
    return prev[n]


def score_dependency_chain(expected: object, actual: object) -> ScoreResult:
    """Score a dependency_chain answer_type using LCS / max(len(expected), len(actual))."""
    exp = (
        [str(s).strip().lower() for s in expected] if isinstance(expected, list) else []
    )
    act = [str(s).strip().lower() for s in actual] if isinstance(actual, list) else []
    max_len = max(len(exp), len(act))
    if max_len == 0:
        return ScoreResult(
            score=0.0,
            passed=False,
            scorer_family="sequence_lcs",
            sub_scores={"lcs_length": 0, "max_len": 0},
        )
    lcs = _lcs_length(exp, act)
    score = lcs / max_len
    return ScoreResult(
        score=score,
        passed=score >= PASS_THRESHOLD,
        scorer_family="sequence_lcs",
        sub_scores={
            "lcs_length": lcs,
            "expected_len": len(exp),
            "actual_len": len(act),
            "max_len": max_len,
        },
    )
