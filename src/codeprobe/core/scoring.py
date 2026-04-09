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
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

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


@dataclass(frozen=True)
class ScoreResult:
    """Result of scoring a task's agent output."""

    score: float
    passed: bool
    error: str | None = None


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


def _safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a filtered environment with only safe keys.

    Prevents secret leakage via inherited environment variables.
    """
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
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
        shutil.copytree(task_dir, sandbox_task, symlinks=False)

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
    """

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
            return ScoreResult(score=clamped, passed=clamped > 0.0)
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
        total_weight = sum(cp.get("weight", 0.0) for cp in checkpoints)
        if abs(total_weight - 1.0) > self._WEIGHT_TOLERANCE:
            return ScoreResult(
                score=0.0,
                passed=False,
                error=f"Checkpoint weights must sum to 1.0, got {total_weight:.4f}",
            )

        weighted_score = 0.0
        for cp in checkpoints:
            weight = cp.get("weight", 0.0)
            verifier_name = cp.get("verifier", "")
            verifier_path = task_dir / "tests" / "verifiers" / verifier_name

            if not verifier_path.is_file():
                return ScoreResult(
                    score=0.0,
                    passed=False,
                    error=f"Verifier not found: {verifier_name}",
                )

            cp_score = self._run_verifier(verifier_path, agent_output, task_dir)
            weighted_score += cp_score * weight

        clamped = max(0.0, min(1.0, weighted_score))
        return ScoreResult(score=clamped, passed=clamped > 0.0)

    @staticmethod
    def _run_verifier(
        verifier_path: Path,
        agent_output: str,
        task_dir: Path,
    ) -> float:
        """Run a single checkpoint verifier and return its score (0.0-1.0)."""
        run = _run_in_sandbox(verifier_path, agent_output, task_dir)
        if run.error is not None:
            return 0.0

        # Try to parse JSON from stdout
        stdout = run.stdout.strip()
        if stdout:
            try:
                data = json.loads(stdout)
                raw = float(data.get("score", 0.0))
                return max(0.0, min(1.0, raw))
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        # Fallback: exit code
        if run.returncode == 0:
            return 1.0
        return 0.0


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


def _load_json_file(path: Path) -> dict | list | None:
    """Safely load a JSON file, returning None on any failure."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))  # type: ignore[no-any-return]
    except (json.JSONDecodeError, OSError):
        return None


def _find_answer_file(task_dir: Path) -> Path | None:
    """Locate the agent's answer.json — try task_dir first, then tests/."""
    for candidate in (task_dir / "answer.json", task_dir / "tests" / "answer.json"):
        if candidate.is_file():
            return candidate
    return None


class ArtifactScorer:
    """Scores agent output by comparing answer.json against ground_truth.json.

    Supports four answer_type variants (new format) and a legacy oracle format.
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
        if "answer_type" in gt:
            return self._score_new_format(gt, answer_data)
        return self._score_legacy_format(gt, answer_data)

    def _score_new_format(self, gt: dict, answer_data: dict) -> ScoreResult:
        answer_type = gt.get("answer_type", "")
        expected = gt.get("answer")
        actual = answer_data.get("answer")

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

        if answer_type == "file_list":
            f1 = self._compute_f1(expected, actual)
            return ScoreResult(score=f1, passed=f1 > 0.0)
        if answer_type == "count":
            return self._score_count(expected, actual)
        if answer_type == "boolean":
            return self._score_boolean(expected, actual)
        if answer_type == "text":
            return self._score_text(expected, actual)

        return ScoreResult(
            score=0.0,
            passed=False,
            error=f"Unknown answer_type: {answer_type!r}",
        )

    def _score_legacy_format(self, gt: dict, answer_data: dict) -> ScoreResult:
        """Legacy format: treat 'expected' as a file_list."""
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
        f1 = self._compute_f1(expected, actual)
        return ScoreResult(score=f1, passed=f1 > 0.0)

    @staticmethod
    def _compute_f1(expected: list[str], actual: list[str]) -> float:
        """Compute F1 score from two lists of file paths."""
        expected_set = frozenset(_normalize_path(p) for p in expected if p)
        actual_set = frozenset(_normalize_path(p) for p in actual if p)
        if not expected_set:
            return 0.0
        if not actual_set:
            return 0.0
        intersection = len(expected_set & actual_set)
        precision = intersection / len(actual_set)
        recall = intersection / len(expected_set)
        if precision + recall == 0:
            return 0.0
        return 2 * precision * recall / (precision + recall)

    @staticmethod
    def _score_count(expected: object, actual: object) -> ScoreResult:
        """Exact integer match."""
        try:
            passed = int(expected) == int(actual)  # type: ignore[arg-type]
        except (ValueError, TypeError):
            return ScoreResult(
                score=0.0,
                passed=False,
                error="count values must be convertible to int",
            )
        return ScoreResult(score=1.0 if passed else 0.0, passed=passed)

    @staticmethod
    def _score_exact_match(expected: object, actual: object) -> ScoreResult:
        """Normalised exact match (strip + lowercase). Used for boolean and text."""
        passed = str(expected).strip().lower() == str(actual).strip().lower()
        return ScoreResult(score=1.0 if passed else 0.0, passed=passed)

    # Aliases for dispatch table readability
    _score_boolean = _score_exact_match
    _score_text = _score_exact_match


# ---------------------------------------------------------------------------
# Registry (delegates to core.registry entry-point resolution)
# ---------------------------------------------------------------------------

from codeprobe.core.registry import available_scorers, resolve_scorer  # noqa: E402

VALID_REWARD_TYPES: frozenset[str] = frozenset(available_scorers())


def get_scorer(
    reward_type: str,
) -> BinaryScorer | ContinuousScorer | CheckpointScorer | ArtifactScorer:
    """Return a Scorer instance for the given reward_type.

    Raises ValueError for unknown reward types (fail loudly — premortem rule).
    """
    try:
        return resolve_scorer(reward_type)
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
