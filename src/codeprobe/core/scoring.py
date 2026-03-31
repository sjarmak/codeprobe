"""Task output scoring — run test.sh and return typed results."""

from __future__ import annotations

import logging
import os
import re
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


def score_task_output(agent_output: str, task_dir: Path) -> ScoreResult:
    """Run tests/test.sh with the agent output and return a ScoreResult.

    Writes agent_output to a temp file and sets the AGENT_OUTPUT env var
    to its path before invoking bash tests/test.sh.
    """
    test_sh = task_dir / "tests" / "test.sh"
    if not test_sh.is_file():
        return ScoreResult(score=0.0, passed=False, error="tests/test.sh not found")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False
    ) as tmp:
        tmp.write(agent_output)
        tmp_path = tmp.name

    try:
        result = subprocess.run(
            ["bash", str(test_sh)],
            env={**os.environ, "AGENT_OUTPUT": tmp_path},
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
        Path(tmp_path).unlink(missing_ok=True)
