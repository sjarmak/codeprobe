"""Tests for core/scoring.py — task output scoring."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from codeprobe.core.scoring import sanitize_secrets, score_task_output


def _make_test_sh(task_dir: Path, script: str) -> None:
    """Write a test.sh script into task_dir/tests/."""
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_sh = tests_dir / "test.sh"
    test_sh.write_text(script)
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)


def test_score_passes_on_exit_zero(tmp_path: Path):
    task_dir = tmp_path / "task-001"
    _make_test_sh(task_dir, '#!/bin/bash\nexit 0\n')

    result = score_task_output("any output", task_dir)
    assert result.score == 1.0
    assert result.passed is True
    assert result.error is None


def test_score_fails_on_exit_nonzero(tmp_path: Path):
    task_dir = tmp_path / "task-002"
    _make_test_sh(task_dir, '#!/bin/bash\nexit 1\n')

    result = score_task_output("wrong answer", task_dir)
    assert result.score == 0.0
    assert result.passed is False


def test_score_reads_agent_output_env(tmp_path: Path):
    task_dir = tmp_path / "task-003"
    # test.sh reads AGENT_OUTPUT file and checks content
    _make_test_sh(task_dir, (
        '#!/bin/bash\n'
        'content=$(cat "$AGENT_OUTPUT")\n'
        'if [ "$content" = "correct" ]; then exit 0; else exit 1; fi\n'
    ))

    result = score_task_output("correct", task_dir)
    assert result.passed is True

    result = score_task_output("wrong", task_dir)
    assert result.passed is False


def test_score_missing_test_sh(tmp_path: Path):
    task_dir = tmp_path / "task-004"
    task_dir.mkdir(parents=True)

    result = score_task_output("output", task_dir)
    assert result.score == 0.0
    assert result.passed is False


def test_sanitize_secrets_redacts_github_tokens():
    text = "Error: token ghp_abcdefghijklmnopqrstuvwxyz1234567890 not valid"
    cleaned = sanitize_secrets(text)
    assert "ghp_" not in cleaned
    assert "[REDACTED]" in cleaned


def test_sanitize_secrets_redacts_pat():
    text = f"github_pat_{'a' * 80} leaked"
    cleaned = sanitize_secrets(text)
    assert "github_pat_" not in cleaned


def test_sanitize_secrets_redacts_openai_key():
    text = "Error: sk-abc123def456ghi789jkl012mno345pq is invalid"
    cleaned = sanitize_secrets(text)
    assert "sk-abc" not in cleaned
    assert "[REDACTED]" in cleaned


def test_sanitize_secrets_redacts_aws_key():
    text = "AKIAIOSFODNN7EXAMPLE leaked"
    cleaned = sanitize_secrets(text)
    assert "AKIA" not in cleaned


def test_sanitize_secrets_redacts_bearer():
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.long.token.here"
    cleaned = sanitize_secrets(text)
    assert "eyJhbGci" not in cleaned


def test_sanitize_secrets_leaves_clean_text():
    text = "normal error message"
    assert sanitize_secrets(text) == text
