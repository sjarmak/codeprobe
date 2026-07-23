"""Scorer classes — Binary, Continuous, Checkpoint, OracleChecks, Artifact,
and Dual — plus ground-truth validation, the oracle answer-type dispatch
table, scorer registry resolution (``get_scorer``), and the scoring CLI.

Split out of the original ``codeprobe/core/scoring.py`` module (pure
mechanical move — see the package ``__init__`` for the public surface).
"""

from __future__ import annotations

import json
import logging
import math
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import cast

from codeprobe.analysis.stats import PASS_THRESHOLD
from codeprobe.core.scoring.ir import (
    _ZERO_SCORE,
    _compute_f1,
    _fbeta,
    _ir_metrics,
    _ir_reward_from_family,
    _normalize_path,
    score_count,
    score_dependency_chain,
    score_exact_match,
    score_file_list,
    score_symbol_list,
)
from codeprobe.core.scoring.materialize import AgentState
from codeprobe.core.scoring.result import (
    DEFAULT_FBETA_BETA,
    DEFAULT_IR_FAMILY,
    Scorer,
    ScoreResult,
    _read_fbeta_beta,
    _select_ir_family,
    read_task_verification,
)
from codeprobe.core.scoring.sandbox import _run_in_sandbox, sanitize_secrets

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Legacy function (preserved for backward compatibility)
# ---------------------------------------------------------------------------


def score_task_output(agent_output: str, task_dir: Path) -> ScoreResult:
    """Run tests/test.sh with the agent output and return a ScoreResult.

    Security measures:
    - Copies task dir to a temp directory (filesystem isolation)
    - Filters environment to safe keys only (secret leak prevention)
    - Sets cwd to the temp copy (cwd isolation)
    - Enforces a 30-second timeout
    """
    return BinaryScorer().score(agent_output, task_dir)


# ---------------------------------------------------------------------------
# BinaryScorer
# ---------------------------------------------------------------------------


class BinaryScorer:
    """Binary pass/fail scorer — exit 0 = 1.0, anything else = 0.0.

    When *agent_state* is passed, the verifier runs against a fresh
    git clone at ``agent_state.base_commit`` with the agent's full
    diff applied — see :func:`_run_in_sandbox` for the materialisation
    contract and Slice 1b (bead codeprobe-xysn) for the rationale.

    Verdict mapping:
      * ``verifier_error=True`` from the sandbox → ``verdict="verifier_error"``.
        Apply rejection means we never ran the test; do NOT grade the agent.
      * ``returncode == 0``                       → ``verdict="correct"``.
      * any other non-error path                  → ``verdict="incorrect"``.
    ``materialized_via`` is propagated from the sandbox unchanged.
    """

    SCORER_FAMILY = "binary_test"

    def score(
        self,
        agent_output: str,
        task_dir: Path,
        *,
        agent_state: AgentState | None = None,
    ) -> ScoreResult:
        test_sh = task_dir / "tests" / "test.sh"
        if not test_sh.is_file():
            return ScoreResult(
                score=0.0,
                passed=False,
                error="tests/test.sh not found",
                scorer_family=self.SCORER_FAMILY,
            )

        run = _run_in_sandbox(
            test_sh, agent_output, task_dir, agent_state=agent_state
        )
        # Containment disclosure: every scored trial reports whether the
        # verifier ran in a container or on the host (codeprobe-f7rl.4).
        exec_details = {"sandbox_execution": run.execution_mode}
        if run.verifier_error:
            # Materialisation pipeline rejected the diff; we never ran
            # test.sh. Surface distinctly from agent-failure so reviewers
            # bucket it as verifier-infrastructure trouble (premortem R1).
            return ScoreResult(
                score=0.0,
                passed=False,
                error=run.error,
                details=exec_details,
                scorer_family=self.SCORER_FAMILY,
                verdict="verifier_error",
                materialized_via=run.materialized_via,
            )
        if run.error is not None:
            return ScoreResult(
                score=0.0,
                passed=False,
                error=run.error,
                details=exec_details,
                scorer_family=self.SCORER_FAMILY,
                materialized_via=run.materialized_via,
            )
        if run.returncode == 0:
            return ScoreResult(
                score=1.0,
                passed=True,
                details=exec_details,
                scorer_family=self.SCORER_FAMILY,
                sub_scores={"exit_code": 0},
                verdict="correct",
                materialized_via=run.materialized_via,
            )
        return ScoreResult(
            score=0.0,
            passed=False,
            error=sanitize_secrets(run.stderr.strip()) if run.stderr else None,
            details=exec_details,
            scorer_family=self.SCORER_FAMILY,
            sub_scores={"exit_code": run.returncode},
            verdict="incorrect",
            materialized_via=run.materialized_via,
        )


# ---------------------------------------------------------------------------
# ContinuousScorer
# ---------------------------------------------------------------------------


def _parse_float_score(raw: str) -> float | None:
    """Try to parse a float from a string, returning None on failure."""
    try:
        val = float(raw.strip())
        if math.isnan(val) or math.isinf(val):
            return None
        return val
    except (ValueError, TypeError):
        return None


class ContinuousScorer:
    """Reads a continuous score (0.0-1.0) from reward.txt or stdout.

    Scoring flow:
    1. Run tests/test.sh in sandbox
    2. If exit code != 0 → score 0.0
    3. Look for reward.txt in the sandbox task dir
    4. Fallback: parse last non-empty line of stdout
    5. Clamp to [0.0, 1.0]
    6. If the oracle wrote ``metrics.json`` (precision/recall/matched/etc.),
       merge it into the result's ``details`` so callers can inspect the
       breakdown without changing the headline F1 score.
    """

    # Whitelist of metrics.json keys we propagate into ScoreResult.details.
    # Anything else the oracle writes is ignored — we don't want a future
    # oracle change to silently widen the result schema.
    _METRICS_WHITELIST = (
        "f1",
        "precision",
        "recall",
        "matched",
        "expected_count",
        "agent_files_count",
        "metric",
        "weighted_recall",
    )

    def score(self, agent_output: str, task_dir: Path) -> ScoreResult:
        test_sh = task_dir / "tests" / "test.sh"
        if not test_sh.is_file():
            return ScoreResult(
                score=0.0,
                passed=False,
                error="tests/test.sh not found",
                scorer_family="continuous",
            )

        run = _run_in_sandbox(test_sh, agent_output, task_dir, cleanup=False)
        # Containment disclosure: every scored trial reports whether the
        # verifier ran in a container or on the host (codeprobe-f7rl.4).
        exec_details = {"sandbox_execution": run.execution_mode}
        try:
            if run.error is not None:
                return ScoreResult(
                    score=0.0,
                    passed=False,
                    error=run.error,
                    details=exec_details,
                    scorer_family="continuous",
                )
            if run.returncode != 0:
                return ScoreResult(
                    score=0.0,
                    passed=False,
                    error=sanitize_secrets(run.stderr.strip()) if run.stderr else None,
                    details=exec_details,
                    scorer_family="continuous",
                    sub_scores={"exit_code": run.returncode},
                )

            # Try reward.txt first
            raw_score = self._read_reward_txt(run.sandbox_task)
            if raw_score is None:
                # Fallback: last non-empty line of stdout
                raw_score = self._parse_stdout(run.stdout)

            if raw_score is None:
                return ScoreResult(
                    score=0.0,
                    passed=False,
                    error="No valid score found in reward.txt or stdout",
                    details=exec_details,
                    scorer_family="continuous",
                )

            clamped = max(0.0, min(1.0, raw_score))
            details = self._read_metrics_json(run.sandbox_task)
            if details:
                # IR-style oracle (file_list / weighted file_list). Family
                # routing reads ``verification.scorer_family`` from
                # task metadata so symbol-reference-trace defaults to F1
                # (over-shipping penalised) while file-discovery tasks can
                # opt into ``oracle_overlap_recall`` (over-shipping is
                # free). See :func:`_select_ir_family` and
                # docs/scoring_model.md for the rubric registry.
                weighted = isinstance(
                    details.get("weighted_recall"), (int, float)
                ) and math.isfinite(float(details["weighted_recall"]))
                family = _select_ir_family(task_dir, weighted=weighted)
                beta = _read_fbeta_beta(task_dir)
                reward, ir_metrics, sub_scores = self._derive_reward_and_metrics(
                    details, fallback=clamped, family=family, beta=beta
                )
                return ScoreResult(
                    score=reward,
                    passed=reward >= PASS_THRESHOLD,
                    details={**details, **exec_details},
                    reward_score=reward,
                    ir_metrics=ir_metrics,
                    scorer_family=family,
                    sub_scores=sub_scores,
                    diagnostics={"ir_metrics": dict(ir_metrics)} if ir_metrics else {},
                )
            # Non-IR continuous: preserve the legacy ``passed = score > 0.0``
            # semantic for tasks whose verifier emits a raw float without
            # an oracle metrics.json. IR scorers use the stricter
            # ``>= PASS_THRESHOLD`` rule above; mixing the two would break
            # custom continuous tasks that intentionally emit sub-threshold
            # rewards as "partial credit, treat as pass".
            return ScoreResult(
                score=clamped,
                passed=clamped > 0.0,
                details=exec_details,
                reward_score=clamped,
                scorer_family="continuous",
                sub_scores={"raw_score": clamped},
            )
        finally:
            if run.sandbox_dir is not None:
                shutil.rmtree(run.sandbox_dir, ignore_errors=True)

    @staticmethod
    def _read_reward_txt(sandbox_task: Path | None) -> float | None:
        if sandbox_task is None:
            return None
        reward_file = sandbox_task / "reward.txt"
        if not reward_file.is_file():
            return None
        return _parse_float_score(reward_file.read_text(encoding="utf-8"))

    @staticmethod
    def _derive_reward_and_metrics(
        details: dict,
        *,
        fallback: float,
        family: str,
        beta: float = DEFAULT_FBETA_BETA,
    ) -> tuple[float, dict, dict]:
        """Derive ``(reward, ir_metrics, sub_scores)`` from an oracle
        ``metrics.json`` under the given scorer ``family``.

        Family-specific reward formulas:

        * ``oracle_overlap_f1`` — reward = f1. Penalises both over-shipping
          and missing. The default for symbol-reference-trace style tasks
          where shipping every file in the repo is "didn't solve". Over-ship
          and under-ship asymmetry shows in sub_scores.
        * ``oracle_overlap_fbeta`` — reward = F-beta(precision, recall;
          beta from ``verification.fbeta_beta``). Equivalent to F1 when
          beta=1; beta<1 favours precision (over-ship costs more).
        * ``oracle_overlap_recall`` — reward = recall. Over-shipping is
          free. The opt-in family for file-discovery / triage tasks.
        * ``oracle_weighted_f1`` — reward = weighted_f1 (read from
          ``f1`` when the oracle is in weighted mode, since the on-disk
          oracle stores its primary score there). Tier weights affect the
          per-file contribution but the rubric still penalises noise.
        * ``oracle_weighted_recall`` — reward = weighted_recall. The tier-
          weighted recall family (was the post-voxa default; now opt-in).
        * Anything else — fall back to ``fallback`` (whatever reward.txt
          said) and surface the family unchanged so the result is still
          interpretable.

        ``ir_metrics`` echoes the precision/recall/f1 (and weighted_recall
        when present) for downstream diagnostics. Values are coerced to
        float and clamped to [0, 1] — defensive, since the oracle script
        is user-modifiable.

        ``sub_scores`` is the rubric breakdown that produced the headline
        — exposes the reward formula's inputs so reviewers can see WHY a
        score landed where it did. Mirrors ``ir_metrics`` for IR families
        but is family-scoped (e.g. doesn't include keys that aren't
        load-bearing for this family).
        """

        def _num(key: str) -> float | None:
            v = details.get(key)
            if isinstance(v, (int, float)) and math.isfinite(float(v)):
                return max(0.0, min(1.0, float(v)))
            return None

        precision = _num("precision")
        recall = _num("recall")
        f1 = _num("f1")
        weighted_recall = _num("weighted_recall")

        ir_metrics: dict[str, float] = {}
        if precision is not None:
            ir_metrics["precision"] = precision
        if recall is not None:
            ir_metrics["recall"] = recall
        if f1 is not None:
            ir_metrics["f1"] = f1
        if weighted_recall is not None:
            ir_metrics["weighted_recall"] = weighted_recall

        # Compute F1 on the fly when the oracle emitted precision/recall
        # but didn't precompute it, so F1 families don't have to fall back.
        if f1 is None and precision is not None and recall is not None:
            f1 = _fbeta(precision, recall, DEFAULT_FBETA_BETA)

        # Pick reward by family. The formula itself lives in
        # _ir_reward_from_family — the single family→reward table — so the
        # metrics.json path and the bare-list path can never diverge. This
        # block only decides whether metrics.json carries what the family's
        # formula needs; when it doesn't, the reward honestly falls back to
        # ``fallback`` (reward.txt) for every family alike. An F1-family
        # reward never silently becomes recall (the voxa-class regression).
        if family == "oracle_overlap_recall":
            usable = recall is not None
        elif family == "oracle_weighted_recall":
            usable = weighted_recall is not None or recall is not None
        elif family == "oracle_overlap_fbeta":
            # The F-beta formula needs precision and recall; a precomputed
            # f1 alone suffices only at β=1 (handled below).
            usable = precision is not None and recall is not None
        else:
            # F1 families (oracle_overlap_f1, oracle_weighted_f1) and
            # unrecognised families.
            usable = f1 is not None

        if usable:
            reward = _ir_reward_from_family(
                family,
                precision=precision if precision is not None else 0.0,
                recall=recall if recall is not None else 0.0,
                f1=f1 if f1 is not None else 0.0,
                beta=beta,
                weighted_recall=weighted_recall,
            )
        elif (
            family == "oracle_overlap_fbeta"
            and f1 is not None
            and beta == DEFAULT_FBETA_BETA
        ):
            # β=1 collapses to F1 — honour the oracle's precomputed value
            # when precision/recall weren't emitted.
            reward = f1
        else:
            reward = max(0.0, min(1.0, fallback))

        sub_scores: dict[str, float] = {}
        if precision is not None:
            sub_scores["precision"] = precision
        if recall is not None:
            sub_scores["recall"] = recall
        if f1 is not None:
            sub_scores["f1"] = f1
        if weighted_recall is not None:
            sub_scores["weighted_recall"] = weighted_recall
        sub_scores["reward"] = reward
        if family == "oracle_overlap_fbeta":
            sub_scores["fbeta_beta"] = beta

        return reward, ir_metrics, sub_scores

    @classmethod
    def _read_metrics_json(cls, sandbox_task: Path | None) -> dict:
        """Pick whitelisted oracle metrics out of ``metrics.json``, if present.

        Returns an empty dict on missing / unreadable / malformed files —
        scoring must stay robust if the oracle script is older or the
        metrics file is absent. We never let metrics-extraction failure
        change the headline score.
        """
        if sandbox_task is None:
            return {}
        metrics_file = sandbox_task / "metrics.json"
        if not metrics_file.is_file():
            return {}
        try:
            payload = json.loads(metrics_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {k: payload[k] for k in cls._METRICS_WHITELIST if k in payload}

    @staticmethod
    def _parse_stdout(stdout: str) -> float | None:
        lines = [ln.strip() for ln in stdout.strip().splitlines() if ln.strip()]
        if not lines:
            return None
        return _parse_float_score(lines[-1])


# ---------------------------------------------------------------------------
# CheckpointScorer
# ---------------------------------------------------------------------------


class CheckpointScorer:
    """Runs weighted checkpoint verifiers and computes a composite score.

    Checkpoint definitions are resolved in order of precedence:

    1. ``metadata_checkpoints`` passed at construction (from task.toml
       ``[[checkpoints]]`` via :class:`~codeprobe.models.task.Checkpoint`)
    2. ``tests/checkpoints.json`` on disk (legacy format)

    Verifier scripts live in ``tests/verifiers/`` and emit JSON on stdout:
    ``{"score": 0.0-1.0, "passed": bool}``

    Fallback: exit 0 = {score: 1.0, passed: true},
              exit nonzero = {score: 0.0, passed: false}
    """

    _WEIGHT_TOLERANCE = 1e-6

    def __init__(
        self,
        metadata_checkpoints: (
            tuple[dict[str, object], ...] | list[dict[str, object]] | None
        ) = None,
    ) -> None:
        self._metadata_checkpoints = metadata_checkpoints

    def _load_checkpoints(
        self, task_dir: Path
    ) -> list[dict[str, object]] | ScoreResult:
        """Resolve checkpoint list — metadata first, then checkpoints.json.

        Returns the list on success or a ``ScoreResult`` error on failure.
        """
        # Prefer metadata checkpoints when provided
        if self._metadata_checkpoints:
            return list(self._metadata_checkpoints)

        # Fall back to on-disk checkpoints.json
        checkpoints_file = task_dir / "tests" / "checkpoints.json"
        if not checkpoints_file.is_file():
            return ScoreResult(
                score=0.0,
                passed=False,
                error="tests/checkpoints.json not found",
                scorer_family="weighted_checkpoints",
            )

        try:
            checkpoints = json.loads(
                checkpoints_file.read_text(encoding="utf-8"),
            )
        except (json.JSONDecodeError, OSError) as exc:
            return ScoreResult(
                score=0.0,
                passed=False,
                error=f"Invalid checkpoints.json: {exc}",
                scorer_family="weighted_checkpoints",
            )
        return checkpoints  # type: ignore[no-any-return]

    def score(self, agent_output: str, task_dir: Path) -> ScoreResult:
        loaded = self._load_checkpoints(task_dir)
        if isinstance(loaded, ScoreResult):
            return loaded
        checkpoints = loaded

        # Validate weights sum to 1.0
        total_weight = sum(
            (
                float(cp.get("weight", 0.0) or 0.0)  # type: ignore[arg-type]
                for cp in checkpoints
            ),
            0.0,
        )
        if abs(total_weight - 1.0) > self._WEIGHT_TOLERANCE:
            return ScoreResult(
                score=0.0,
                passed=False,
                error=f"Checkpoint weights must sum to 1.0, got {total_weight:.4f}",
                scorer_family="weighted_checkpoints",
            )

        weighted_score = 0.0
        # Per-checkpoint breakdown propagated via ScoreResult.details so
        # the executor can surface it in scoring.json and the interpret
        # report can show partial-credit columns (see R17).
        checkpoint_scores: dict[str, float] = {}
        checkpoint_weights: dict[str, float] = {}
        sandbox_execution = ""

        for cp in checkpoints:
            weight = float(cp.get("weight", 0.0) or 0.0)  # type: ignore[arg-type]
            verifier_name = str(cp.get("verifier", "") or "")
            name = str(cp.get("name", verifier_name) or verifier_name)
            verifier_path = task_dir / "tests" / "verifiers" / verifier_name

            if not verifier_path.is_file():
                return ScoreResult(
                    score=0.0,
                    passed=False,
                    error=f"Verifier not found: {verifier_name}",
                    scorer_family="weighted_checkpoints",
                )

            cp_score, sandbox_execution = self._run_verifier(
                verifier_path, agent_output, task_dir
            )
            weighted_score += cp_score * weight
            checkpoint_scores[name] = cp_score
            checkpoint_weights[name] = weight

        clamped = max(0.0, min(1.0, weighted_score))
        return ScoreResult(
            score=clamped,
            passed=clamped >= PASS_THRESHOLD,
            details={
                "checkpoint_scores": checkpoint_scores,
                "checkpoint_weights": checkpoint_weights,
                # Containment disclosure (codeprobe-f7rl.4). All verifiers
                # in one trial run in the same environment; the last mode
                # observed stands for the trial.
                "sandbox_execution": sandbox_execution,
            },
            scorer_family="weighted_checkpoints",
            sub_scores={
                "composite": clamped,
                "checkpoint_scores": dict(checkpoint_scores),
            },
        )

    @staticmethod
    def _run_verifier(
        verifier_path: Path,
        agent_output: str,
        task_dir: Path,
    ) -> tuple[float, str]:
        """Run a single checkpoint verifier.

        Returns ``(score, execution_mode)`` where score is 0.0-1.0 and
        execution_mode is the sandbox's ``"container"`` / ``"host"`` /
        ``"none"`` (nothing executed — refusal or setup failure).
        """
        run = _run_in_sandbox(verifier_path, agent_output, task_dir)
        if run.error is not None:
            # R16: fail loud. Sandbox already logged the root cause at
            # WARNING; surface the verifier-level context so the reader
            # can trace which checkpoint degraded.
            logger.warning(
                "Verifier %s produced zero score due to sandbox error: %s",
                verifier_path.name,
                run.error,
            )
            return _ZERO_SCORE, run.execution_mode

        # Try to parse JSON from stdout
        stdout = run.stdout.strip()
        if stdout:
            try:
                data = json.loads(stdout)
                raw = float(data.get("score", 0.0))
                return max(0.0, min(1.0, raw)), run.execution_mode
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Fallback: exit code. Non-zero is a legitimate "verifier failed"
        # signal (not a silent swallow); returncode is the loud channel.
        if run.returncode == 0:
            return 1.0, run.execution_mode
        return _ZERO_SCORE, run.execution_mode


# ---------------------------------------------------------------------------
# OracleChecksScorer
# ---------------------------------------------------------------------------


class OracleChecksScorer:
    """Structured-rubric scorer — per-criterion verifiers with weighted average.

    Mirrors the CSB ``oracle_checks`` pattern: each task declares a list of
    named criteria, each with a weight and a verifier script. Verifiers run
    independently in the sandbox and emit ``{"score": 0.0-1.0, "passed": bool}``
    JSON or fall back to exit-code semantics. The headline reward is the
    weight-normalized average ``Σ(weight_i · score_i) / Σ(weight_i)``.

    Differences from :class:`CheckpointScorer`:

    * Weights are *normalized* rather than required to sum to ``1.0``. A
      rubric with weights ``[2, 1, 1]`` is equivalent to ``[0.5, 0.25,
      0.25]``. This makes incremental rubric edits cheap — adding a
      criterion doesn't force re-balancing every other weight.
    * Reports ``scorer_family = "oracle_checks"`` so reviewers can
      distinguish "verifier evaluated a structured rubric" from
      ``weighted_checkpoints`` (which historically labels both
      ``CheckpointScorer`` and ``ArtifactScorer._score_v2_checks`` —
      ambiguous semantics that this family disentangles).

    Rubric source resolution order:

    1. ``metadata_criteria`` constructor argument — populated from
       ``task.toml [[rubric_criteria]]`` by the task loader.
    2. ``tests/rubric.json`` on disk — for tasks that ship the rubric
       inline. JSON shape: a list of ``{"name", "weight", "verifier"}``
       objects (``description`` is optional and ignored by the scorer).

    Verifier scripts live in ``tests/verifiers/`` (same layout as
    ``CheckpointScorer``) and emit JSON on stdout: ``{"score": 0.0-1.0,
    "passed": bool}``. Fallback: exit ``0`` → ``1.0``, nonzero → ``0.0``.
    """

    SCORER_FAMILY = "oracle_checks"

    def __init__(
        self,
        metadata_criteria: (
            tuple[dict[str, object], ...] | list[dict[str, object]] | None
        ) = None,
    ) -> None:
        self._metadata_criteria = metadata_criteria

    def _load_criteria(
        self, task_dir: Path
    ) -> list[dict[str, object]] | ScoreResult:
        """Resolve criteria list — metadata first, then tests/rubric.json.

        Returns the list on success or a ``ScoreResult`` error on failure.
        """
        if self._metadata_criteria:
            return list(self._metadata_criteria)

        rubric_file = task_dir / "tests" / "rubric.json"
        if not rubric_file.is_file():
            return ScoreResult(
                score=0.0,
                passed=False,
                error="tests/rubric.json not found",
                scorer_family=self.SCORER_FAMILY,
            )

        try:
            payload = json.loads(rubric_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return ScoreResult(
                score=0.0,
                passed=False,
                error=f"Invalid rubric.json: {exc}",
                scorer_family=self.SCORER_FAMILY,
            )

        if not isinstance(payload, list):
            return ScoreResult(
                score=0.0,
                passed=False,
                error="rubric.json must be a JSON list of criteria",
                scorer_family=self.SCORER_FAMILY,
            )
        return cast(list[dict[str, object]], payload)

    def score(self, agent_output: str, task_dir: Path) -> ScoreResult:
        loaded = self._load_criteria(task_dir)
        if isinstance(loaded, ScoreResult):
            return loaded
        criteria = loaded

        if not criteria:
            return ScoreResult(
                score=0.0,
                passed=False,
                error="rubric must declare at least one criterion",
                scorer_family=self.SCORER_FAMILY,
            )

        # Validate weights up-front. A negative or non-finite weight is a
        # rubric authoring bug, not a missed criterion — fail loudly so
        # reviewers see the typo in metadata rather than a silently
        # truncated reward.
        weights: list[float] = []
        for idx, crit in enumerate(criteria):
            raw = crit.get("weight", 0.0) if isinstance(crit, dict) else 0.0
            try:
                w = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return ScoreResult(
                    score=0.0,
                    passed=False,
                    error=f"criterion[{idx}] weight is not numeric: {raw!r}",
                    scorer_family=self.SCORER_FAMILY,
                )
            if not math.isfinite(w):
                return ScoreResult(
                    score=0.0,
                    passed=False,
                    error=f"criterion[{idx}] weight must be finite, got: {w}",
                    scorer_family=self.SCORER_FAMILY,
                )
            if w < 0.0:
                return ScoreResult(
                    score=0.0,
                    passed=False,
                    error=f"criterion[{idx}] weight must be non-negative, got: {w}",
                    scorer_family=self.SCORER_FAMILY,
                )
            weights.append(w)

        total_weight = sum(weights, 0.0)
        if total_weight <= 0.0:
            return ScoreResult(
                score=0.0,
                passed=False,
                error=(
                    "rubric weights sum to zero — at least one criterion "
                    "must have a positive weight"
                ),
                scorer_family=self.SCORER_FAMILY,
            )

        criterion_scores: dict[str, float] = {}
        criterion_weights: dict[str, float] = {}
        weighted_sum = 0.0
        sandbox_execution = ""

        for idx, (crit, weight) in enumerate(zip(criteria, weights, strict=True)):
            verifier_name = str(crit.get("verifier", "") or "")
            name = str(crit.get("name", verifier_name) or verifier_name) or f"criterion_{idx}"

            if not verifier_name:
                return ScoreResult(
                    score=0.0,
                    passed=False,
                    error=f"criterion[{idx}] missing 'verifier' field",
                    scorer_family=self.SCORER_FAMILY,
                )

            verifier_path = task_dir / "tests" / "verifiers" / verifier_name
            if not verifier_path.is_file():
                return ScoreResult(
                    score=0.0,
                    passed=False,
                    error=f"Verifier not found: {verifier_name}",
                    scorer_family=self.SCORER_FAMILY,
                )

            cp_score, sandbox_execution = self._run_verifier(
                verifier_path, agent_output, task_dir
            )
            weighted_sum += cp_score * weight
            criterion_scores[name] = cp_score
            criterion_weights[name] = weight

        # Normalize by total weight (the family's defining property).
        reward = weighted_sum / total_weight
        reward = max(0.0, min(1.0, reward))

        sub_scores: dict[str, object] = {
            "composite": reward,
            "criterion_scores": dict(criterion_scores),
            "total_weight": total_weight,
        }
        return ScoreResult(
            score=reward,
            passed=reward >= PASS_THRESHOLD,
            details={
                "criterion_scores": criterion_scores,
                "criterion_weights": criterion_weights,
                "total_weight": total_weight,
                # Containment disclosure (codeprobe-f7rl.4) — same
                # convention as CheckpointScorer.
                "sandbox_execution": sandbox_execution,
            },
            scorer_family=self.SCORER_FAMILY,
            sub_scores=sub_scores,
        )

    @staticmethod
    def _run_verifier(
        verifier_path: Path,
        agent_output: str,
        task_dir: Path,
    ) -> tuple[float, str]:
        """Run a single criterion verifier.

        Returns ``(score, execution_mode)`` with score in 0.0-1.0. Mirrors
        :meth:`CheckpointScorer._run_verifier` so both composite scorers
        see the same sandbox semantics: JSON ``score`` field is preferred,
        exit-code is the documented fallback.
        """
        run = _run_in_sandbox(verifier_path, agent_output, task_dir)
        if run.error is not None:
            logger.warning(
                "Verifier %s produced zero score due to sandbox error: %s",
                verifier_path.name,
                run.error,
            )
            return _ZERO_SCORE, run.execution_mode

        stdout = run.stdout.strip()
        if stdout:
            try:
                data = json.loads(stdout)
                raw = float(data.get("score", 0.0))
                return max(0.0, min(1.0, raw)), run.execution_mode
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        if run.returncode == 0:
            return 1.0, run.execution_mode
        return _ZERO_SCORE, run.execution_mode


# ---------------------------------------------------------------------------
# ArtifactScorer
# ---------------------------------------------------------------------------

_MAX_GROUND_TRUTH_BYTES = 10 * 1024 * 1024  # 10 MB

# The confidence warning threshold (default 0.5, historical value) is a
# policy value sourced from ``ExperimentConfig.low_confidence_threshold``
# (codeprobe-kdng) and passed as the ``low_confidence_threshold`` keyword
# argument to :meth:`ArtifactScorer.score` below — not a module constant,
# so it can be tuned per experiment without editing scorer code.


def _load_json_file(path: Path) -> dict | list | None:
    """Safely load a JSON file, returning None on any failure.

    Rejects files larger than ``_MAX_GROUND_TRUTH_BYTES`` to prevent OOM
    on malicious or accidentally oversized ground_truth.json files.
    """
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
        if size > _MAX_GROUND_TRUTH_BYTES:
            logger.warning(
                "JSON file too large (%d bytes, limit %d): %s",
                size,
                _MAX_GROUND_TRUTH_BYTES,
                path,
            )
            return None
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None


def _find_answer_file(task_dir: Path) -> Path | None:
    """Locate the agent's answer.json — try task_dir first, then tests/."""
    for candidate in (task_dir / "answer.json", task_dir / "tests" / "answer.json"):
        if candidate.is_file():
            return candidate
    return None


_ORACLE_TYPE_SCORERS: dict[str, Callable[..., ScoreResult]] = {
    "file_list": score_file_list,
    "count": score_count,
    "boolean": score_exact_match,
    "text": score_exact_match,
    "symbol_list": score_symbol_list,
    "dependency_chain": score_dependency_chain,
}

# answer_types whose scorer signature accepts the ``family`` keyword. Used
# by ArtifactScorer to pass the per-task IR family through; everything else
# uses the (expected, actual) signature unchanged.
_IR_LIST_ANSWER_TYPES: frozenset[str] = frozenset({"file_list", "symbol_list"})


_WEIGHT_TOLERANCE = 1e-6


def validate_ground_truth(gt: dict) -> str | None:
    """Validate a ground_truth.json dict. Returns None if valid, error string if not.

    Supports three formats:
    - V2: ``checks`` array with weighted multi-check scoring
    - V1: ``answer_type`` + ``answer`` single-answer scoring
    - Legacy: ``expected`` as a list
    """
    if "checks" in gt:
        checks = gt["checks"]
        if not isinstance(checks, list) or len(checks) == 0:
            return "v2 ground_truth 'checks' must be a non-empty list"
        for i, check in enumerate(checks):
            if not isinstance(check, dict):
                return f"check[{i}] must be a dict"
            if "answer_type" not in check:
                return f"check[{i}] missing 'answer_type'"
            if "answer" not in check:
                return f"check[{i}] missing 'answer'"
            if "weight" not in check:
                return f"check[{i}] missing 'weight'"
            weight = check["weight"]
            try:
                w = float(weight)
            except (TypeError, ValueError):
                return f"check[{i}] weight is not numeric: {weight!r}"
            if not math.isfinite(w):
                return f"check[{i}] weight must be finite, got: {w}"
            if w < 0.0 or w > 1.0:
                return f"check[{i}] weight out of range [0, 1]: {w}"
        total = sum(float(c["weight"]) for c in checks)
        if abs(total - 1.0) > _WEIGHT_TOLERANCE:
            return f"check weights must sum to 1.0, got {total:.6f}"
        return None

    if "answer_type" in gt:
        if "answer" not in gt:
            return "v1 ground_truth has 'answer_type' but missing 'answer'"
        answer_type = gt["answer_type"]
        answer = gt["answer"]
        # Validate answer shape matches declared answer_type
        if answer_type in ("file_list", "symbol_list", "dependency_chain"):
            if not isinstance(answer, list):
                return (
                    f"v1 ground_truth answer_type {answer_type!r} requires a list, "
                    f"got {type(answer).__name__}"
                )
        elif answer_type == "count":
            try:
                int(answer)
            except (ValueError, TypeError):
                return (
                    f"v1 ground_truth answer_type 'count' requires an int-convertible value, "
                    f"got {type(answer).__name__}: {answer!r}"
                )
        elif answer_type in ("boolean", "text"):
            if not isinstance(answer, (str, bool, int, float)):
                return (
                    f"v1 ground_truth answer_type {answer_type!r} requires a scalar value, "
                    f"got {type(answer).__name__}"
                )
        return None

    if "expected" in gt:
        if not isinstance(gt["expected"], list):
            return "legacy ground_truth 'expected' must be a list"
        return None

    return (
        "ground_truth.json must have 'checks' (v2), "
        "'answer_type' (v1), or 'expected' (legacy)"
    )


class ArtifactScorer:
    """Scores agent output by comparing answer.json against ground_truth.json.

    Supports three formats:
    - V2: ``checks`` array with weighted multi-check scoring
    - V1: single ``answer_type`` + ``answer``
    - Legacy: ``expected`` file list

    ``score`` accepts an optional ``low_confidence_threshold`` keyword
    (default 0.5, the historical hardcoded value) sourced from
    ``ExperimentConfig.low_confidence_threshold`` (codeprobe-kdng). The
    executor detects support structurally via
    :func:`scorer_accepts_low_confidence_threshold`, mirroring the
    ``agent_state`` opt-in pattern.
    """

    def score(
        self,
        agent_output: str,
        task_dir: Path,
        *,
        low_confidence_threshold: float = 0.5,
    ) -> ScoreResult:
        # Resolve the IR family up front so error-path ScoreResults can
        # declare the rubric the caller requested. Verifier-honesty lint
        # (tests/lint/test_scorer_honesty.py) requires every ScoreResult
        # constructor to carry a scorer_family declaration.
        ir_family = _select_ir_family(task_dir)
        ir_beta = _read_fbeta_beta(task_dir)

        # Load ground truth — check tests/ subdir first (standard location),
        # then task_dir root (legacy). Keep in sync with mining/writer._ORACLE_PY.
        gt_path = task_dir / "tests" / "ground_truth.json"
        if not gt_path.exists():
            gt_path = task_dir / "ground_truth.json"
        gt = _load_json_file(gt_path)
        if gt is None or not isinstance(gt, dict):
            # Oracle-side failure: a missing or corrupt answer key is
            # verifier infrastructure breaking, not the agent failing the
            # task. Route to verdict="verifier_error" so it can never be
            # silently collapsed into an agent loss (premortem Theme C).
            detail = (
                "ground_truth.json is invalid or oversized"
                if gt_path.is_file()
                else "ground_truth.json not found"
            )
            return ScoreResult(
                score=0.0,
                passed=False,
                error=detail,
                scorer_family=ir_family,
                verdict="verifier_error",
            )

        # Warn on low-confidence ground truth
        confidence = gt.get("confidence")
        if confidence is not None and confidence < low_confidence_threshold:
            logger.warning(
                "Low confidence ground truth (%.2f) in %s",
                confidence,
                gt_path,
            )

        # Load agent answer
        answer_path = _find_answer_file(task_dir)
        if answer_path is None:
            return ScoreResult(
                score=0.0,
                passed=False,
                error="answer.json not found",
                scorer_family=ir_family,
            )
        answer_data = _load_json_file(answer_path)
        if answer_data is None or not isinstance(answer_data, dict):
            return ScoreResult(
                score=0.0,
                passed=False,
                error="answer.json is invalid JSON",
                scorer_family=ir_family,
            )

        # Detect format and dispatch. The IR family declared in
        # task metadata applies to all IR-style sub-scorers (file_list /
        # symbol_list / legacy file-list); non-IR scorers (count / boolean
        # / text / dependency_chain) ignore it.
        if "checks" in gt:
            return self._score_v2_checks(
                gt, answer_data, ir_family=ir_family, ir_beta=ir_beta
            )
        if "answer_type" in gt:
            return self._score_new_format(
                gt, answer_data, ir_family=ir_family, ir_beta=ir_beta
            )
        return self._score_legacy_format(
            gt, answer_data, ir_family=ir_family, ir_beta=ir_beta
        )

    def _score_v2_checks(
        self,
        gt: dict,
        answer_data: dict,
        *,
        ir_family: str = DEFAULT_IR_FAMILY,
        ir_beta: float = DEFAULT_FBETA_BETA,
    ) -> ScoreResult:
        """Score using v2 multi-check format with weighted composite."""
        checks: list[dict] = gt.get("checks", [])

        # Validate structure — a malformed oracle is a verifier-side
        # failure, not an agent failure.
        validation_error = validate_ground_truth(gt)
        if validation_error is not None:
            return ScoreResult(
                score=0.0,
                passed=False,
                error=validation_error,
                scorer_family="weighted_checkpoints",
                verdict="verifier_error",
            )

        # Build answer lookup: {answer_type: answer_value} from agent answers.
        # Use the first occurrence of each answer_type (spec: "first match").
        answer_lookup: dict[str, object] = {}
        raw_answers = answer_data.get("answers")
        if isinstance(raw_answers, list):
            for entry in raw_answers:
                if isinstance(entry, dict):
                    atype = entry.get("answer_type", "")
                    if atype and atype not in answer_lookup:
                        answer_lookup[atype] = entry.get("answer")
        elif "answer" in answer_data and "answer_type" in answer_data:
            # V1-style answer.json fallback: single answer mapped by its type
            answer_lookup[answer_data["answer_type"]] = answer_data["answer"]

        composite = 0.0
        check_scores: list[dict] = []

        for check in checks:
            answer_type = check["answer_type"]
            expected = check["answer"]
            weight = float(check["weight"])

            # Look up scorer function
            scorer_fn = _ORACLE_TYPE_SCORERS.get(answer_type)
            if scorer_fn is None:
                # Try entry_point registry
                try:
                    from codeprobe.core.registry import resolve_oracle_scorer

                    scorer_fn = resolve_oracle_scorer(answer_type)
                except KeyError:
                    pass

            # Look up agent's answer for this type
            actual = answer_lookup.get(answer_type)

            if scorer_fn is None:
                # Unknown answer_type — scores 0.0 for this check. The
                # family is the answer_type itself so callers can see
                # which check failed.
                check_result = ScoreResult(
                    score=0.0,
                    passed=False,
                    error=f"Unknown answer_type: {answer_type!r}",
                    scorer_family=str(answer_type),
                )
            elif actual is None:
                # Agent didn't provide an answer for this type
                check_result = ScoreResult(
                    score=0.0,
                    passed=False,
                    scorer_family=str(answer_type),
                )
            elif answer_type in _IR_LIST_ANSWER_TYPES:
                # IR scorers take a family kwarg so the v2 multi-check
                # composite respects per-task scorer routing. ``beta`` is
                # only consumed by ``oracle_overlap_fbeta`` but threading
                # it unconditionally keeps the dispatch table simple.
                check_result = scorer_fn(
                    expected, actual, family=ir_family, beta=ir_beta
                )
            else:
                check_result = scorer_fn(expected, actual)

            composite += check_result.score * weight
            check_scores.append(
                {
                    "answer_type": answer_type,
                    "weight": weight,
                    "score": check_result.score,
                    "passed": check_result.passed,
                    **({"error": check_result.error} if check_result.error else {}),
                }
            )

        composite = max(0.0, min(1.0, composite))
        return ScoreResult(
            score=composite,
            passed=composite >= PASS_THRESHOLD,
            details={"check_scores": check_scores},
            scorer_family="weighted_checkpoints",
            sub_scores={"composite": composite, "ir_family": ir_family},
        )

    def _score_new_format(
        self,
        gt: dict,
        answer_data: dict,
        *,
        ir_family: str = DEFAULT_IR_FAMILY,
        ir_beta: float = DEFAULT_FBETA_BETA,
    ) -> ScoreResult:
        answer_type = gt.get("answer_type", "")
        expected = gt.get("answer")
        actual = answer_data.get("answer")

        # Warn on answer_type mismatch (non-fatal — agents may omit it)
        agent_answer_type = answer_data.get("answer_type")
        if agent_answer_type is not None and agent_answer_type != answer_type:
            logger.warning(
                "answer_type mismatch: ground_truth has %r but agent returned %r",
                answer_type,
                agent_answer_type,
            )

        # Family for this scoring path: the answer_type's natural family
        # when known (file_list / symbol_list use ir_family; non-IR types
        # use exact_match), otherwise the answer_type itself.
        if answer_type in _IR_LIST_ANSWER_TYPES:
            error_family = ir_family
        elif isinstance(answer_type, str) and answer_type:
            error_family = str(answer_type)
        else:
            error_family = ""

        if expected is None:
            # Oracle-side failure → verifier_error, not an agent loss.
            return ScoreResult(
                score=0.0,
                passed=False,
                error="ground_truth.json missing 'answer' field",
                scorer_family=error_family,
                verdict="verifier_error",
            )

        if actual is None:
            return ScoreResult(
                score=0.0,
                passed=False,
                error="answer.json missing 'answer' field",
                scorer_family=error_family,
            )

        # Look up in builtin registry first
        scorer_fn = _ORACLE_TYPE_SCORERS.get(answer_type)
        if scorer_fn is not None:
            if answer_type in _IR_LIST_ANSWER_TYPES:
                return scorer_fn(expected, actual, family=ir_family, beta=ir_beta)
            return scorer_fn(expected, actual)

        # Fall back to entry_point registry for extensibility
        try:
            from codeprobe.core.registry import resolve_oracle_scorer

            scorer_fn = resolve_oracle_scorer(answer_type)
            return cast(ScoreResult, scorer_fn(expected, actual))
        except KeyError:
            pass

        return ScoreResult(
            score=0.0,
            passed=False,
            error=f"Unknown answer_type: {answer_type!r}",
            scorer_family=error_family,
        )

    def _score_legacy_format(
        self,
        gt: dict,
        answer_data: dict,
        *,
        ir_family: str = DEFAULT_IR_FAMILY,
        ir_beta: float = DEFAULT_FBETA_BETA,
    ) -> ScoreResult:
        """Legacy format: treat 'expected' as a file_list.

        Honors the same scorer_family routing as :func:`score_file_list`.
        Default family is ``oracle_overlap_f1`` (F1 reward); tasks opt
        into recall-tilted via ``verification.scorer_family``.
        """
        expected = gt.get("expected", [])
        actual = answer_data.get("answer", [])
        if not isinstance(expected, list):
            # Oracle-side failure → verifier_error, not an agent loss.
            return ScoreResult(
                score=0.0,
                passed=False,
                error="Legacy ground_truth.json 'expected' is not a list",
                scorer_family=ir_family,
                verdict="verifier_error",
            )
        if not isinstance(actual, list):
            return ScoreResult(
                score=0.0,
                passed=False,
                error="answer.json 'answer' is not a list",
                scorer_family=ir_family,
            )
        expected_set = frozenset(_normalize_path(p) for p in expected if p)
        actual_set = frozenset(_normalize_path(p) for p in actual if p)
        precision, recall, f1 = _ir_metrics(expected_set, actual_set)
        ir_metrics = {"precision": precision, "recall": recall, "f1": f1}
        reward = _ir_reward_from_family(
            ir_family, precision=precision, recall=recall, f1=f1, beta=ir_beta
        )
        sub_scores: dict = {**ir_metrics, "reward": reward}
        if ir_family == "oracle_overlap_fbeta":
            sub_scores["fbeta_beta"] = ir_beta
        return ScoreResult(
            score=reward,
            passed=reward >= PASS_THRESHOLD,
            details=dict(ir_metrics),
            reward_score=reward,
            ir_metrics=ir_metrics,
            scorer_family=ir_family,
            sub_scores=sub_scores,
            diagnostics={"ir_metrics": dict(ir_metrics)},
        )

    # Delegate to module-level functions (kept for backward compat)
    _compute_f1 = staticmethod(_compute_f1)
    _score_count = staticmethod(score_count)
    _score_exact_match = staticmethod(score_exact_match)

    # Aliases for dispatch table readability
    _score_boolean = _score_exact_match
    _score_text = _score_exact_match


# ---------------------------------------------------------------------------
# DualScorer
# ---------------------------------------------------------------------------


def _safe_leg_score(
    scorer: object,
    agent_output: str,
    task_dir: Path,
    **kwargs: float,
) -> ScoreResult:
    """Invoke a sub-scorer, catching exceptions so both legs always run.

    DualScorer must never short-circuit because one leg raises. Any
    exception is converted into a ScoreResult(score=0.0) with the
    exception message exposed via ``error``. ``kwargs`` forwards
    leg-specific options (e.g. ``low_confidence_threshold`` for the
    artifact leg) — callers pass only what the target scorer accepts.
    """
    try:
        score_fn = getattr(scorer, "score", None)
        if score_fn is None:
            raise AttributeError(f"{type(scorer).__name__!r} has no .score method")
        return cast(ScoreResult, score_fn(agent_output, task_dir, **kwargs))
    except Exception as exc:  # noqa: BLE001 — both legs must run
        scorer_name = type(scorer).__name__
        logger.exception(
            "Scorer %s failed on task_dir=%s",
            scorer_name,
            task_dir,
        )
        return ScoreResult(
            score=0.0,
            passed=False,
            error=f"scorer raised: {type(exc).__name__}: {exc}",
            scorer_family="dual_composite",
        )


class DualScorer:
    """Composes a direct scorer (binary/continuous) with an artifact scorer.

    Runs BOTH legs unconditionally — no early return on failure. Reads
    configuration from ``task_dir/metadata.json`` at score() time so the
    registry can instantiate this class with no arguments and the executor
    can invoke it through the standard Scorer Protocol signature
    ``score(agent_output, task_dir)``.

    Scoring policies:
      - ``""`` (default): ``score = score_direct``
      - ``"min"``: ``score = min(score_direct, score_artifact)``
      - ``"mean"``: ``score = (score_direct + score_artifact) / 2``
      - ``"gate"``: ``1.0`` if both legs pass, else ``0.0``
      - ``"weighted"``: ``score = weight_direct * score_direct
                                 + weight_artifact * score_artifact``

    Graceful degradation:
      - Missing ``tests/test.sh``: direct leg returns 0.0 with an error;
        artifact leg runs normally.
      - Missing ``answer.json``: artifact leg returns 0.0 with an error;
        direct leg runs normally.
      - Missing or unparseable ``metadata.json``: returns score 0.0 with
        an error — dual tasks require valid verification metadata.

    ``score`` accepts an optional ``low_confidence_threshold`` keyword,
    forwarded to the internal artifact leg (codeprobe-kdng).
    """

    def __init__(self) -> None:
        # No config — everything is read from task_dir/metadata.json at score() time.
        pass

    @staticmethod
    def _parse_weight(raw: object, default: float) -> tuple[float, str | None]:
        """Coerce a weight value to a finite float in ``[0.0, 1.0]``.

        Returns ``(weight, error_message)``. Malformed or out-of-range
        weights propagate as an error instead of silently falling back to
        a default — the caller decides whether that's fatal for the
        current scoring_policy.
        """
        if raw is None:
            return default, None
        try:
            value = float(raw)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default, f"invalid weight value: {raw!r}"
        if not math.isfinite(value):
            return default, f"non-finite weight: {raw!r}"
        if value < 0.0 or value > 1.0:
            return default, f"weight out of range [0,1]: {value}"
        return value, None

    def score(
        self,
        agent_output: str,
        task_dir: Path,
        *,
        low_confidence_threshold: float = 0.5,
    ) -> ScoreResult:
        verification = read_task_verification(task_dir)
        if not verification:
            return ScoreResult(
                score=0.0,
                passed=False,
                error=(
                    "dual task verification block missing — metadata.json "
                    "absent, unparseable, or has no verification key"
                ),
                details={"error_metadata": "verification_block_empty"},
                scorer_family="dual_composite",
            )
        reward_type = verification.get("reward_type", "binary") or "binary"
        scoring_policy = verification.get("scoring_policy", "") or ""
        weight_direct, weight_direct_error = self._parse_weight(
            verification.get("weight_direct"), 0.5
        )
        weight_artifact, weight_artifact_error = self._parse_weight(
            verification.get("weight_artifact"), 0.5
        )

        direct_scorer: BinaryScorer | ContinuousScorer
        if reward_type == "continuous":
            direct_scorer = ContinuousScorer()
        else:
            direct_scorer = BinaryScorer()
        artifact_scorer = ArtifactScorer()

        direct_result = _safe_leg_score(direct_scorer, agent_output, task_dir)
        artifact_result = _safe_leg_score(
            artifact_scorer,
            agent_output,
            task_dir,
            low_confidence_threshold=low_confidence_threshold,
        )

        details: dict = {
            "score_direct": direct_result.score,
            "score_artifact": artifact_result.score,
            "passed_direct": direct_result.passed,
            "passed_artifact": artifact_result.passed,
            "scoring_policy": scoring_policy,
        }
        if direct_result.error:
            details["error_direct"] = direct_result.error
        if artifact_result.error:
            details["error_artifact"] = artifact_result.error

        weight_errors: list[str] = []
        if scoring_policy == "weighted":
            if weight_direct_error:
                weight_errors.append(f"weight_direct: {weight_direct_error}")
            if weight_artifact_error:
                weight_errors.append(f"weight_artifact: {weight_artifact_error}")
            if weight_errors:
                details["error_weights"] = "; ".join(weight_errors)
            else:
                details["weight_direct"] = weight_direct
                details["weight_artifact"] = weight_artifact

        if scoring_policy == "min":
            composite = min(direct_result.score, artifact_result.score)
        elif scoring_policy == "mean":
            composite = (direct_result.score + artifact_result.score) / 2.0
        elif scoring_policy == "gate":
            composite = (
                1.0 if (direct_result.passed and artifact_result.passed) else 0.0
            )
        elif scoring_policy == "weighted":
            if weight_errors:
                # Invalid weights are a scoring error — fail closed rather
                # than silently falling back to defaults and masking the bug.
                composite = 0.0
            else:
                composite = (
                    weight_direct * direct_result.score
                    + weight_artifact * artifact_result.score
                )
        else:
            composite = direct_result.score

        composite = max(0.0, min(1.0, composite))
        passed = composite >= PASS_THRESHOLD

        error_parts = [
            f"direct: {direct_result.error}" if direct_result.error else None,
            f"artifact: {artifact_result.error}" if artifact_result.error else None,
            f"weights: {'; '.join(weight_errors)}" if weight_errors else None,
        ]
        combined_error = "; ".join(p for p in error_parts if p) or None

        return ScoreResult(
            score=composite,
            passed=passed,
            error=combined_error,
            details=details,
            scorer_family="dual_composite",
            sub_scores={
                "composite": composite,
                "score_direct": direct_result.score,
                "score_artifact": artifact_result.score,
                "scoring_policy": scoring_policy,
            },
        )


# ---------------------------------------------------------------------------
# Registry (delegates to core.registry entry-point resolution)
# ---------------------------------------------------------------------------

from codeprobe.core.registry import available_scorers, resolve_scorer  # noqa: E402

VALID_REWARD_TYPES: frozenset[str] = frozenset(available_scorers())


def get_scorer(
    reward_type: str,
) -> Scorer:
    """Return a Scorer instance for the given reward_type.

    Raises ValueError for unknown reward types (fail loudly — premortem rule).
    """
    try:
        return cast(Scorer, resolve_scorer(reward_type))
    except KeyError:
        raise ValueError(
            f"Unknown reward_type: {reward_type!r}. "
            f"Expected one of: {sorted(VALID_REWARD_TYPES)}"
        )


# ---------------------------------------------------------------------------
# CLI entry point: python -m codeprobe.core.scoring --artifact <task_dir>
# ---------------------------------------------------------------------------


def _cli_main() -> None:
    """Entry point for ``python -m codeprobe.core.scoring --artifact <dir>``."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Score agent output for a task directory.",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        required=True,
        help="Task directory containing answer.json and ground_truth.json",
    )
    args = parser.parse_args()

    task_dir: Path = args.artifact
    if not task_dir.is_dir():
        print(f"ERROR: {task_dir} is not a directory", file=sys.stderr)
        sys.exit(1)

    scorer = ArtifactScorer()
    result = scorer.score("", task_dir)
    print(
        json.dumps(
            {"score": result.score, "passed": result.passed, "error": result.error}
        )
    )
    sys.exit(0)
