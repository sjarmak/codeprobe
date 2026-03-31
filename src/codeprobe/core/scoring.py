"""Task output scoring — run test.sh and return typed results."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(
    r"("
    r"ghp_[A-Za-z0-9]{36}"           # GitHub personal access token
    r"|gho_[A-Za-z0-9]{36}"          # GitHub OAuth token
    r"|github_pat_[A-Za-z0-9_]{80,}" # GitHub fine-grained PAT
    r"|sk-[A-Za-z0-9]{32,}"          # OpenAI / Anthropic API key
    r"|sk-ant-[A-Za-z0-9\-]{80,}"    # Anthropic API key (long form)
    r"|AKIA[0-9A-Z]{16}"             # AWS access key ID
    r"|Bearer\s+\S{20,}"             # Authorization bearer tokens
    r"|token\s+\S{20,}"              # Generic token patterns
    r")",
    re.IGNORECASE,
)

SCORE_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class ScoreResult:
    """Result of scoring a task's agent output."""

    score: float
    passed: bool
    error: str | None = None


def sanitize_secrets(text: str) -> str:
    """Redact potential secrets (API keys, tokens) from text."""
    return _TOKEN_PATTERN.sub("[REDACTED]", text)


_SAFE_ENV_KEYS = frozenset({"PATH", "HOME", "LANG", "TERM", "TMPDIR", "LC_ALL"})


def _safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a filtered environment with only safe keys.

    Prevents secret leakage via inherited environment variables.
    """
    env = {k: v for k, v in os.environ.items() if k in _SAFE_ENV_KEYS}
    if extra:
        env.update(extra)
    return env


def score_task_output(agent_output: str, task_dir: Path) -> ScoreResult:
    """Run tests/test.sh with the agent output and return a ScoreResult.

    Security measures:
    - Copies task dir to a temp directory (filesystem isolation)
    - Filters environment to safe keys only (secret leak prevention)
    - Sets cwd to the temp copy (cwd isolation)
    - Enforces a 30-second timeout
    """
    test_sh = task_dir / "tests" / "test.sh"
    if not test_sh.is_file():
        return ScoreResult(score=0.0, passed=False, error="tests/test.sh not found")

    sandbox_dir = None
    try:
        # Copy task directory to a temp sandbox (symlinks=True to avoid
        # following symlinks that point outside the task tree)
        sandbox_dir = Path(tempfile.mkdtemp(prefix="codeprobe-score-"))
        sandbox_task = sandbox_dir / "task"
        shutil.copytree(task_dir, sandbox_task, symlinks=True)
        sandbox_test_sh = sandbox_task / "tests" / "test.sh"

        # Write agent output inside the sandbox boundary
        output_file = sandbox_dir / "agent_output.txt"
        output_file.write_text(agent_output, encoding="utf-8")

        env = _safe_env({"AGENT_OUTPUT": str(output_file)})

        result = subprocess.run(
            ["bash", str(sandbox_test_sh)],
            env=env,
            cwd=str(sandbox_task),
            capture_output=True,
            text=True,
            timeout=SCORE_TIMEOUT_SECONDS,
        )
        if result.returncode == 0:
            return ScoreResult(score=1.0, passed=True)
        return ScoreResult(
            score=0.0,
            passed=False,
            error=sanitize_secrets(result.stderr.strip()) if result.stderr else None,
        )
    except subprocess.TimeoutExpired:
        return ScoreResult(score=0.0, passed=False, error="Scoring timed out")
    except OSError as exc:
        return ScoreResult(score=0.0, passed=False, error=str(exc))
    finally:
        if sandbox_dir is not None:
            shutil.rmtree(sandbox_dir, ignore_errors=True)
