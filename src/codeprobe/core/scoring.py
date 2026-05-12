"""Task output scoring — run test.sh and return typed results.

Provides a Scorer protocol with three implementations:
- BinaryScorer: exit 0 = 1.0, else 0.0 (wraps legacy score_task_output)
- ContinuousScorer: reads float from reward.txt or stdout (0.0-1.0)
- CheckpointScorer: weighted checkpoint verifiers with partial credit

All scorers inherit the same sandbox security: temp dir isolation, filtered
environment, secret redaction, and configurable timeout.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, cast, runtime_checkable

from codeprobe.analysis.stats import PASS_THRESHOLD

logger = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(
    r"("
    r"ghp_[A-Za-z0-9]{36}"  # GitHub personal access token
    r"|gho_[A-Za-z0-9]{36}"  # GitHub OAuth token
    r"|github_pat_[A-Za-z0-9_]{80,}"  # GitHub fine-grained PAT
    r"|sk-[A-Za-z0-9]{32,}"  # OpenAI / Anthropic API key
    r"|sk-ant-[A-Za-z0-9\-]{80,}"  # Anthropic API key (long form)
    r"|AKIA[0-9A-Z]{16}"  # AWS access key ID
    r"|Bearer\s+\S{20,}"  # Authorization bearer tokens
    r"|token\s+\S{20,}"  # Generic token patterns
    r")",
    re.IGNORECASE,
)

SCORE_TIMEOUT_SECONDS = 300

# Named constant for zero-score returns — ensures every zero path is
# either (a) legitimate arithmetic (F1 with empty sets) or (b) paired
# with an explicit logger.warning (R16: fail-loud, no silent fallbacks).
_ZERO_SCORE: float = 0.0

# Patterns excluded from sandbox copytree to keep per-task IO bounded.
# Any future task format that legitimately needs one of these paths
# should override this at the writer level, not suppress it here.
_COPYTREE_IGNORE = (
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "target",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
)


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
    contract field name. The two are always equal by definition; keep
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
    working. Today the two are equal by definition.

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
        # ``reward`` mirrors ``score`` when callers don't pass it explicitly.
        # Frozen dataclass requires ``object.__setattr__`` to populate the
        # default after init.
        if self.reward is None:
            object.__setattr__(self, "reward", self.score)
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
    """

    def score(self, agent_output: str, task_dir: Path) -> ScoreResult: ...


# ---------------------------------------------------------------------------
# Shared sandbox helpers
# ---------------------------------------------------------------------------


def sanitize_secrets(text: str) -> str:
    """Redact potential secrets (API keys, tokens) from text."""
    return _TOKEN_PATTERN.sub("[REDACTED]", text)


_SAFE_ENV_KEYS = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "TERM",
        "TMPDIR",
        "LC_ALL",
        # Go toolchain
        "GOPATH",
        "GOROOT",
        "GOMODCACHE",
        "GOCACHE",
        "GOFLAGS",
        # Rust toolchain
        "CARGO_HOME",
        "RUSTUP_HOME",
        # Node/npm
        "NODE_PATH",
        "NPM_CONFIG_PREFIX",
        # Python
        "VIRTUAL_ENV",
        "PYTHONPATH",
    }
)


# Thread-local env overrides for sandboxed scorer subprocesses. Callers use
# :func:`scorer_env_override` as a context manager to bind extra env vars
# (e.g. ``TASK_REPO_ROOT`` for dual tasks) so test.sh can cd into a
# per-run worktree instead of the shared mined repo_path. Raw threads
# each get their own override — no cross-thread leakage.
_scorer_env_tls = threading.local()


def _thread_env_overrides() -> dict[str, str]:
    return getattr(_scorer_env_tls, "overrides", None) or {}


@contextmanager
def scorer_env_override(overrides: dict[str, str] | None) -> Iterator[None]:
    """Bind a thread-local env overlay visible to sandboxed scorer processes.

    ``overrides`` is merged into the filtered env built by :func:`_safe_env`.
    The previous overlay is restored on exit, so nested overrides compose
    in LIFO order.
    """
    previous = _thread_env_overrides()
    _scorer_env_tls.overrides = dict(overrides) if overrides else {}
    try:
        yield
    finally:
        _scorer_env_tls.overrides = previous


def _safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a filtered environment with only safe keys.

    Prevents secret leakage via inherited environment variables. Any
    thread-local overrides bound via :func:`scorer_env_override` are merged
    on top of the filtered env, and the caller's ``extra`` takes highest
    precedence.
    """
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
    env.update(_thread_env_overrides())
    if extra:
        env.update(extra)
    return env


@dataclass(frozen=True)
class _SandboxRun:
    """Result of running a script inside the sandbox."""

    returncode: int
    stdout: str
    stderr: str
    sandbox_dir: Path | None = None
    error: str | None = None

    @property
    def sandbox_task(self) -> Path | None:
        return self.sandbox_dir / "task" if self.sandbox_dir else None


def _run_in_sandbox(
    script_path: Path,
    agent_output: str,
    task_dir: Path,
    *,
    timeout: int | None = None,
    cleanup: bool = True,
) -> _SandboxRun:
    """Execute *script_path* inside a sandboxed copy of *task_dir*.

    Returns a _SandboxRun with process results and paths into the sandbox
    so callers can inspect files written by the script.  When *cleanup* is
    True the sandbox is removed before returning; set to False when the
    caller needs to read sandbox artefacts (caller must clean up).
    """
    if timeout is None:
        timeout = SCORE_TIMEOUT_SECONDS
    sandbox_dir = None
    try:
        sandbox_dir = Path(tempfile.mkdtemp(prefix="codeprobe-score-"))
        sandbox_task = sandbox_dir / "task"
        shutil.copytree(
            task_dir,
            sandbox_task,
            symlinks=False,
            ignore=shutil.ignore_patterns(*_COPYTREE_IGNORE),
        )

        rel = script_path.relative_to(task_dir)
        sandbox_script = sandbox_task / rel

        output_file = sandbox_dir / "agent_output.txt"
        output_file.write_text(agent_output, encoding="utf-8")

        env = _safe_env({"AGENT_OUTPUT": str(output_file)})

        result = subprocess.run(
            ["bash", str(sandbox_script)],
            env=env,
            cwd=str(sandbox_task),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if cleanup:
            shutil.rmtree(sandbox_dir, ignore_errors=True)
            sandbox_dir = None
        return _SandboxRun(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            sandbox_dir=sandbox_dir,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        if sandbox_dir is not None:
            shutil.rmtree(sandbox_dir, ignore_errors=True)
        if isinstance(exc, subprocess.TimeoutExpired):
            error = "Scoring timed out"
        else:
            error = str(exc)
            logger.warning("Sandbox setup failed (OSError): %s", error)
        return _SandboxRun(returncode=-1, stdout="", stderr="", error=error)


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
    """Binary pass/fail scorer — exit 0 = 1.0, anything else = 0.0."""

    SCORER_FAMILY = "binary_test"

    def score(self, agent_output: str, task_dir: Path) -> ScoreResult:
        test_sh = task_dir / "tests" / "test.sh"
        if not test_sh.is_file():
            return ScoreResult(
                score=0.0,
                passed=False,
                error="tests/test.sh not found",
                scorer_family=self.SCORER_FAMILY,
            )

        run = _run_in_sandbox(test_sh, agent_output, task_dir)
        if run.error is not None:
            return ScoreResult(
                score=0.0,
                passed=False,
                error=run.error,
                scorer_family=self.SCORER_FAMILY,
            )
        if run.returncode == 0:
            return ScoreResult(
                score=1.0,
                passed=True,
                scorer_family=self.SCORER_FAMILY,
                sub_scores={"exit_code": 0},
            )
        return ScoreResult(
            score=0.0,
            passed=False,
            error=sanitize_secrets(run.stderr.strip()) if run.stderr else None,
            scorer_family=self.SCORER_FAMILY,
            sub_scores={"exit_code": run.returncode},
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
        try:
            if run.error is not None:
                return ScoreResult(
                    score=0.0,
                    passed=False,
                    error=run.error,
                    scorer_family="continuous",
                )
            if run.returncode != 0:
                return ScoreResult(
                    score=0.0,
                    passed=False,
                    error=sanitize_secrets(run.stderr.strip()) if run.stderr else None,
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
                    details=details,
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

        # Pick reward by family
        if family == "oracle_overlap_recall":
            reward = recall if recall is not None else max(0.0, min(1.0, fallback))
        elif family == "oracle_overlap_fbeta":
            if precision is not None and recall is not None:
                reward = _fbeta(precision, recall, beta)
            elif f1 is not None and beta == DEFAULT_FBETA_BETA:
                # beta=1 collapses to F1; honour the precomputed value.
                reward = f1
            else:
                reward = max(0.0, min(1.0, fallback))
        elif family == "oracle_weighted_recall":
            if weighted_recall is not None:
                reward = weighted_recall
            elif recall is not None:
                reward = recall
            else:
                reward = max(0.0, min(1.0, fallback))
        elif family == "oracle_weighted_f1":
            # On-disk oracle stores weighted_f1 in the ``f1`` field when
            # ``metric == "weighted_f1"`` (see mining/writer.py:_ORACLE_PY).
            # Fall back to recall, then to reward.txt, in that order.
            if f1 is not None:
                reward = f1
            elif weighted_recall is not None:
                reward = weighted_recall
            elif recall is not None:
                reward = recall
            else:
                reward = max(0.0, min(1.0, fallback))
        else:
            # Default IR family — F1 (penalises over-ship and under-ship)
            if f1 is not None:
                reward = f1
            elif recall is not None and precision is not None:
                # Compute on the fly when oracle didn't precompute F1
                reward = (
                    2 * precision * recall / (precision + recall)
                    if (precision + recall) > 0
                    else 0.0
                )
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

            cp_score = self._run_verifier(verifier_path, agent_output, task_dir)
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
    ) -> float:
        """Run a single checkpoint verifier and return its score (0.0-1.0)."""
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
            return _ZERO_SCORE

        # Try to parse JSON from stdout
        stdout = run.stdout.strip()
        if stdout:
            try:
                data = json.loads(stdout)
                raw = float(data.get("score", 0.0))
                return max(0.0, min(1.0, raw))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Fallback: exit code. Non-zero is a legitimate "verifier failed"
        # signal (not a silent swallow); returncode is the loud channel.
        if run.returncode == 0:
            return 1.0
        return _ZERO_SCORE


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

            cp_score = self._run_verifier(verifier_path, agent_output, task_dir)
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
            },
            scorer_family=self.SCORER_FAMILY,
            sub_scores=sub_scores,
        )

    @staticmethod
    def _run_verifier(
        verifier_path: Path,
        agent_output: str,
        task_dir: Path,
    ) -> float:
        """Run a single criterion verifier and return its score (0.0-1.0).

        Mirrors :meth:`CheckpointScorer._run_verifier` so both composite
        scorers see the same sandbox semantics: JSON ``score`` field is
        preferred, exit-code is the documented fallback.
        """
        run = _run_in_sandbox(verifier_path, agent_output, task_dir)
        if run.error is not None:
            logger.warning(
                "Verifier %s produced zero score due to sandbox error: %s",
                verifier_path.name,
                run.error,
            )
            return _ZERO_SCORE

        stdout = run.stdout.strip()
        if stdout:
            try:
                data = json.loads(stdout)
                raw = float(data.get("score", 0.0))
                return max(0.0, min(1.0, raw))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        if run.returncode == 0:
            return 1.0
        return _ZERO_SCORE


# ---------------------------------------------------------------------------
# ArtifactScorer
# ---------------------------------------------------------------------------


def _normalize_path(p: str) -> str:
    """Normalize a file path for comparison — strip prefixes and separators."""
    p = p.replace("\\", "/").strip()
    for pfx in ("./", "/workspace/", "/tmp/", "/app/"):
        while p.startswith(pfx):
            p = p[len(pfx) :]
    return p.lstrip("/")


_MAX_GROUND_TRUTH_BYTES = 10 * 1024 * 1024  # 10 MB

# Confidence below which we log a warning about low-confidence ground
# truth in :class:`ArtifactScorer`. Promoted from an inline literal so
# verifier-honesty lint sees a named, documented constant rather than a
# bare ``< 0.5`` in scoring code (see tests/lint/test_scorer_honesty.py).
_LOW_CONFIDENCE_THRESHOLD: float = 0.5


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
) -> float:
    """Derive an IR reward from precision/recall/f1 under ``family``.

    Centralised so :func:`score_file_list`, :func:`score_symbol_list`, and
    the legacy file-list scorer share one formula table. Falls back to F1
    for any unrecognised family — the conservative choice since over-
    shipping should generally cost something.

    ``beta`` is consumed only by the ``oracle_overlap_fbeta`` family. The
    default (1.0) makes that family numerically equivalent to F1; callers
    that want a different bias supply the value resolved from per-task
    metadata via :func:`_read_fbeta_beta`.
    """
    if family == "oracle_overlap_recall":
        return recall
    if family == "oracle_weighted_recall":
        return recall  # IR list scorers don't see tier weights; tier-weight
        # families surface via ContinuousScorer over the on-disk oracle
    if family == "oracle_overlap_fbeta":
        return _fbeta(precision, recall, beta)
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
    """

    def score(self, agent_output: str, task_dir: Path) -> ScoreResult:
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
            return ScoreResult(
                score=0.0,
                passed=False,
                error="ground_truth.json not found or invalid",
                scorer_family=ir_family,
            )

        # Warn on low-confidence ground truth
        confidence = gt.get("confidence")
        if confidence is not None and confidence < _LOW_CONFIDENCE_THRESHOLD:
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

        # Validate structure
        validation_error = validate_ground_truth(gt)
        if validation_error is not None:
            return ScoreResult(
                score=0.0,
                passed=False,
                error=validation_error,
                scorer_family="weighted_checkpoints",
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
            return ScoreResult(
                score=0.0,
                passed=False,
                error="ground_truth.json missing 'answer' field",
                scorer_family=error_family,
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
            return ScoreResult(
                score=0.0,
                passed=False,
                error="Legacy ground_truth.json 'expected' is not a list",
                scorer_family=ir_family,
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
) -> ScoreResult:
    """Invoke a sub-scorer, catching exceptions so both legs always run.

    DualScorer must never short-circuit because one leg raises. Any
    exception is converted into a ScoreResult(score=0.0) with the
    exception message exposed via ``error``.
    """
    try:
        score_fn = getattr(scorer, "score", None)
        if score_fn is None:
            raise AttributeError(f"{type(scorer).__name__!r} has no .score method")
        return cast(ScoreResult, score_fn(agent_output, task_dir))
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
        artifact_result = _safe_leg_score(artifact_scorer, agent_output, task_dir)

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


if __name__ == "__main__":
    _cli_main()
