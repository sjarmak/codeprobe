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


@dataclass(frozen=True)
class ScoreResult:
    """Result of scoring a task's agent output.

    ``score`` is the headline reward — the number that drives ranking,
    pass/fail, and ``mean_automated_score`` in aggregate.json. For IR-style
    scorers (``file_list``, ``symbol_list``, oracle continuous) reward is
    oracle-matching (recall, or weighted_recall when the oracle uses tier
    weights); precision and F1 are computed alongside but do **not** drag
    the reward down.

    ``ir_metrics`` exposes those IR diagnostics (``precision`` / ``recall``
    / ``f1`` and optional ``weighted_recall``) so callers can inspect the
    over-shipping vs under-shipping shape without re-computing it. Empty
    for non-IR scorers (binary, exact_match, count, etc.).

    ``reward_score`` mirrors ``score`` for now and is provided so future
    schema migrations can disambiguate "score the user reads" from
    "headline metric for ranking" without another contract break. Today
    the two are equal by definition.

    ``details`` continues to carry the precision/recall/f1 fields for
    backward compatibility with aggregate.json consumers that read
    ``scoring_details["f1"]`` directly. Treat ``ir_metrics`` as the
    canonical source going forward.
    """

    score: float
    passed: bool
    error: str | None = None
    details: dict = field(default_factory=dict)
    reward_score: float | None = None
    ir_metrics: dict = field(default_factory=dict)


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

    def score(self, agent_output: str, task_dir: Path) -> ScoreResult:
        test_sh = task_dir / "tests" / "test.sh"
        if not test_sh.is_file():
            return ScoreResult(score=0.0, passed=False, error="tests/test.sh not found")

        run = _run_in_sandbox(test_sh, agent_output, task_dir)
        if run.error is not None:
            return ScoreResult(score=0.0, passed=False, error=run.error)
        if run.returncode == 0:
            return ScoreResult(score=1.0, passed=True)
        return ScoreResult(
            score=0.0,
            passed=False,
            error=sanitize_secrets(run.stderr.strip()) if run.stderr else None,
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
            return ScoreResult(score=0.0, passed=False, error="tests/test.sh not found")

        run = _run_in_sandbox(test_sh, agent_output, task_dir, cleanup=False)
        try:
            if run.error is not None:
                return ScoreResult(score=0.0, passed=False, error=run.error)
            if run.returncode != 0:
                return ScoreResult(
                    score=0.0,
                    passed=False,
                    error=sanitize_secrets(run.stderr.strip()) if run.stderr else None,
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
                )

            clamped = max(0.0, min(1.0, raw_score))
            details = self._read_metrics_json(run.sandbox_task)
            if details:
                # IR-style oracle (file_list / weighted file_list): reward
                # is recall (or weighted_recall when the oracle uses tier
                # weights). The headline ``score`` shifts off F1 onto
                # oracle-matching so over-shipping no longer drags the
                # reward down. F1 / precision stay in ``ir_metrics`` for
                # diagnostics. See docs/scoring_model.md.
                reward, ir_metrics = self._derive_reward_and_metrics(
                    details, fallback=clamped
                )
                return ScoreResult(
                    score=reward,
                    passed=reward > 0.0,
                    details=details,
                    reward_score=reward,
                    ir_metrics=ir_metrics,
                )
            return ScoreResult(
                score=clamped,
                passed=clamped > 0.0,
                reward_score=clamped,
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
        details: dict, *, fallback: float
    ) -> tuple[float, dict]:
        """Derive ``(reward, ir_metrics)`` from an oracle ``metrics.json``.

        The oracle today writes ``reward.txt = f1`` (or ``weighted_f1``)
        for legacy reasons. We pivot to oracle-matching here so the
        scoring contract stays decoupled from the on-disk script:

        * If ``weighted_recall`` is present and finite → reward = it
          (matches the org-scale weighted oracle's intent that "tier-A
          hits matter more than tier-C hits, and over-shipping is free").
        * Otherwise if ``recall`` is present → reward = recall.
        * Otherwise fall back to the parsed reward.txt value (``fallback``)
          so older mined tasks without recall in their metrics still get
          a sensible score.

        ``ir_metrics`` echoes the precision/recall/f1 (and weighted_recall
        when present) for downstream diagnostics. Values are coerced to
        float and clamped to [0, 1] — defensive, since the oracle script
        is user-modifiable.
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

        if weighted_recall is not None:
            reward = weighted_recall
        elif recall is not None:
            reward = recall
        else:
            reward = max(0.0, min(1.0, fallback))

        ir_metrics: dict[str, float] = {}
        if precision is not None:
            ir_metrics["precision"] = precision
        if recall is not None:
            ir_metrics["recall"] = recall
        if f1 is not None:
            ir_metrics["f1"] = f1
        if weighted_recall is not None:
            ir_metrics["weighted_recall"] = weighted_recall
        return reward, ir_metrics

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
                )

            cp_score = self._run_verifier(verifier_path, agent_output, task_dir)
            weighted_score += cp_score * weight
            checkpoint_scores[name] = cp_score
            checkpoint_weights[name] = weight

        clamped = max(0.0, min(1.0, weighted_score))
        return ScoreResult(
            score=clamped,
            passed=clamped > 0.0,
            details={
                "checkpoint_scores": checkpoint_scores,
                "checkpoint_weights": checkpoint_weights,
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


def score_file_list(expected: object, actual: object) -> ScoreResult:
    """Score a file_list answer_type.

    Reward is oracle-matching (recall): did the agent find every expected
    file?  Precision and F1 are reported alongside in ``ir_metrics`` for
    diagnostics but do not drag the reward down. Over-shipping shows up as
    low precision, not as a reward penalty.
    """
    if not isinstance(expected, list):
        return ScoreResult(
            score=0.0,
            passed=False,
            error=f"file_list expected answer must be a list, got {type(expected).__name__}",
        )
    if not isinstance(actual, list):
        return ScoreResult(
            score=0.0,
            passed=False,
            error=f"file_list actual answer must be a list, got {type(actual).__name__}",
        )
    expected_set = frozenset(_normalize_path(p) for p in expected if p)
    actual_set = frozenset(_normalize_path(p) for p in actual if p)
    precision, recall, f1 = _ir_metrics(expected_set, actual_set)
    ir_metrics = {"precision": precision, "recall": recall, "f1": f1}
    return ScoreResult(
        score=recall,
        passed=recall >= PASS_THRESHOLD,
        details=dict(ir_metrics),
        reward_score=recall,
        ir_metrics=ir_metrics,
    )


def score_count(expected: object, actual: object) -> ScoreResult:
    """Exact integer match."""
    try:
        passed = int(expected) == int(actual)  # type: ignore[call-overload]
    except (ValueError, TypeError):
        return ScoreResult(
            score=0.0,
            passed=False,
            error="count values must be convertible to int",
        )
    return ScoreResult(score=1.0 if passed else 0.0, passed=passed)


def score_exact_match(expected: object, actual: object) -> ScoreResult:
    """Normalised exact match (strip + lowercase). Used for boolean and text."""
    passed = str(expected).strip().lower() == str(actual).strip().lower()
    return ScoreResult(score=1.0 if passed else 0.0, passed=passed)


def _normalize_symbol(s: str) -> str:
    """Normalize a symbol name for comparison.

    Strips module prefixes (split on '.' and '::'), lowercases, and strips
    whitespace.  E.g. ``"foo.bar.MyClass"`` -> ``"myclass"``.
    """
    # Split on '::' first, take last segment, then split on '.', take last
    s = s.split("::")[-1].split(".")[-1]
    return s.strip().lower()


def score_symbol_list(expected: object, actual: object) -> ScoreResult:
    """Score a symbol_list answer_type.

    Reward is recall over normalized symbol names; precision and F1 are
    reported in ``ir_metrics``. See :func:`score_file_list` for rationale.
    """
    exp = expected if isinstance(expected, list) else []
    act = actual if isinstance(actual, list) else []
    exp_set = frozenset(_normalize_symbol(str(s)) for s in exp if s)
    act_set = frozenset(_normalize_symbol(str(s)) for s in act if s)
    if not exp_set or not act_set:
        empty = {"precision": 0.0, "recall": 0.0, "f1": 0.0}
        return ScoreResult(
            score=0.0,
            passed=False,
            details=dict(empty),
            reward_score=0.0,
            ir_metrics=empty,
        )
    precision, recall, f1 = _ir_metrics(exp_set, act_set)
    ir_metrics = {"precision": precision, "recall": recall, "f1": f1}
    return ScoreResult(
        score=recall,
        passed=recall >= PASS_THRESHOLD,
        details=dict(ir_metrics),
        reward_score=recall,
        ir_metrics=ir_metrics,
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
        return ScoreResult(score=0.0, passed=False)
    lcs = _lcs_length(exp, act)
    score = lcs / max_len
    return ScoreResult(score=score, passed=score >= PASS_THRESHOLD)


_ORACLE_TYPE_SCORERS: dict[str, Callable[[object, object], ScoreResult]] = {
    "file_list": score_file_list,
    "count": score_count,
    "boolean": score_exact_match,
    "text": score_exact_match,
    "symbol_list": score_symbol_list,
    "dependency_chain": score_dependency_chain,
}


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
            )

        # Warn on low-confidence ground truth
        confidence = gt.get("confidence")
        if confidence is not None and confidence < 0.5:
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
            )
        answer_data = _load_json_file(answer_path)
        if answer_data is None or not isinstance(answer_data, dict):
            return ScoreResult(
                score=0.0,
                passed=False,
                error="answer.json is invalid JSON",
            )

        # Detect format and dispatch
        if "checks" in gt:
            return self._score_v2_checks(gt, answer_data)
        if "answer_type" in gt:
            return self._score_new_format(gt, answer_data)
        return self._score_legacy_format(gt, answer_data)

    def _score_v2_checks(self, gt: dict, answer_data: dict) -> ScoreResult:
        """Score using v2 multi-check format with weighted composite."""
        checks: list[dict] = gt.get("checks", [])

        # Validate structure
        validation_error = validate_ground_truth(gt)
        if validation_error is not None:
            return ScoreResult(score=0.0, passed=False, error=validation_error)

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
                # Unknown answer_type — scores 0.0 for this check
                check_result = ScoreResult(
                    score=0.0,
                    passed=False,
                    error=f"Unknown answer_type: {answer_type!r}",
                )
            elif actual is None:
                # Agent didn't provide an answer for this type
                check_result = ScoreResult(score=0.0, passed=False)
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
        )

    def _score_new_format(self, gt: dict, answer_data: dict) -> ScoreResult:
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

        if expected is None:
            return ScoreResult(
                score=0.0,
                passed=False,
                error="ground_truth.json missing 'answer' field",
            )

        if actual is None:
            return ScoreResult(
                score=0.0,
                passed=False,
                error="answer.json missing 'answer' field",
            )

        # Look up in builtin registry first
        scorer_fn = _ORACLE_TYPE_SCORERS.get(answer_type)
        if scorer_fn is not None:
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
        )

    def _score_legacy_format(self, gt: dict, answer_data: dict) -> ScoreResult:
        """Legacy format: treat 'expected' as a file_list.

        Same reward shape as :func:`score_file_list` — recall is the headline,
        precision/F1 are diagnostics in ``ir_metrics``.
        """
        expected = gt.get("expected", [])
        actual = answer_data.get("answer", [])
        if not isinstance(expected, list):
            return ScoreResult(
                score=0.0,
                passed=False,
                error="Legacy ground_truth.json 'expected' is not a list",
            )
        if not isinstance(actual, list):
            return ScoreResult(
                score=0.0,
                passed=False,
                error="answer.json 'answer' is not a list",
            )
        expected_set = frozenset(_normalize_path(p) for p in expected if p)
        actual_set = frozenset(_normalize_path(p) for p in actual if p)
        precision, recall, f1 = _ir_metrics(expected_set, actual_set)
        ir_metrics = {"precision": precision, "recall": recall, "f1": f1}
        return ScoreResult(
            score=recall,
            passed=recall > 0.0,
            details=dict(ir_metrics),
            reward_score=recall,
            ir_metrics=ir_metrics,
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
