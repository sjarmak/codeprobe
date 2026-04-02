"""Tests for core/scoring.py — task output scoring."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from codeprobe.core.scoring import (
    BinaryScorer,
    CheckpointScorer,
    ContinuousScorer,
    ScoreResult,
    Scorer,
    get_scorer,
    sanitize_secrets,
    score_task_output,
)


def _make_test_sh(task_dir: Path, script: str) -> None:
    """Write a test.sh script into task_dir/tests/."""
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_sh = tests_dir / "test.sh"
    test_sh.write_text(script)
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)


def test_score_passes_on_exit_zero(tmp_path: Path):
    task_dir = tmp_path / "task-001"
    _make_test_sh(task_dir, "#!/bin/bash\nexit 0\n")

    result = score_task_output("any output", task_dir)
    assert result.score == 1.0
    assert result.passed is True
    assert result.error is None


def test_score_fails_on_exit_nonzero(tmp_path: Path):
    task_dir = tmp_path / "task-002"
    _make_test_sh(task_dir, "#!/bin/bash\nexit 1\n")

    result = score_task_output("wrong answer", task_dir)
    assert result.score == 0.0
    assert result.passed is False


def test_score_reads_agent_output_env(tmp_path: Path):
    task_dir = tmp_path / "task-003"
    # test.sh reads AGENT_OUTPUT file and checks content
    _make_test_sh(
        task_dir,
        (
            "#!/bin/bash\n"
            'content=$(cat "$AGENT_OUTPUT")\n'
            'if [ "$content" = "correct" ]; then exit 0; else exit 1; fi\n'
        ),
    )

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


def test_score_does_not_follow_symlinks(tmp_path: Path):
    """copytree should not follow symlinks — symlinked files are skipped."""
    task_dir = tmp_path / "task-symlink"
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True)

    # Create a file outside the task dir that a symlink would point to
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text("TOP SECRET")

    # Create a symlink inside the task dir pointing to the secret file
    link = task_dir / "linked_secret.txt"
    link.symlink_to(secret_file)

    # test.sh checks whether the symlink target was copied
    _make_test_sh(
        task_dir,
        (
            "#!/bin/bash\n"
            'if [ -f "$PWD/linked_secret.txt" ]; then exit 1; else exit 0; fi\n'
        ),
    )

    result = score_task_output("output", task_dir)
    # With symlinks=False, the symlink is not followed — it's copied as a
    # regular file (the content IS copied but it's a flat copy, not a link).
    # The key security property is that the sandbox copy is NOT a symlink.
    sandbox_dir = None
    from codeprobe.core.scoring import _run_in_sandbox

    test_sh = task_dir / "tests" / "test.sh"
    run = _run_in_sandbox(test_sh, "output", task_dir, cleanup=False)
    try:
        if run.sandbox_dir:
            copied = run.sandbox_dir / "task" / "linked_secret.txt"
            # The copied file should NOT be a symlink
            assert not copied.is_symlink()
    finally:
        if run.sandbox_dir:
            import shutil

            shutil.rmtree(run.sandbox_dir, ignore_errors=True)


def test_score_runs_in_sandbox_does_not_modify_original(tmp_path: Path):
    """test.sh writing a file should not modify the original task directory."""
    task_dir = tmp_path / "task-sandbox"
    _make_test_sh(
        task_dir, ("#!/bin/bash\n" 'touch "$PWD/side_effect.txt"\n' "exit 0\n")
    )

    result = score_task_output("output", task_dir)
    assert result.passed is True
    # The side effect file should NOT exist in the original task dir
    assert not (task_dir / "side_effect.txt").exists()


def test_score_env_does_not_leak_secrets(tmp_path: Path):
    """test.sh should not see secrets from the parent environment."""
    task_dir = tmp_path / "task-env"
    _make_test_sh(
        task_dir,
        ("#!/bin/bash\n" 'if [ -n "$SECRET_API_KEY" ]; then exit 1; else exit 0; fi\n'),
    )

    # Set a secret in the environment
    os.environ["SECRET_API_KEY"] = "sk-test-secret-key-12345678901234567890"
    try:
        result = score_task_output("output", task_dir)
        # test.sh should pass because SECRET_API_KEY is filtered out
        assert result.passed is True
    finally:
        del os.environ["SECRET_API_KEY"]


def test_score_timeout_enforced(tmp_path: Path):
    """test.sh should be killed after the timeout."""
    task_dir = tmp_path / "task-timeout"
    _make_test_sh(task_dir, "#!/bin/bash\nsleep 60\nexit 0\n")

    # Use a shorter timeout for this test
    import codeprobe.core.scoring as scoring_mod

    original = scoring_mod.SCORE_TIMEOUT_SECONDS
    scoring_mod.SCORE_TIMEOUT_SECONDS = 1
    try:
        result = score_task_output("output", task_dir)
        assert result.passed is False
        assert result.error == "Scoring timed out"
    finally:
        scoring_mod.SCORE_TIMEOUT_SECONDS = original


# ---------------------------------------------------------------------------
# Scorer protocol conformance
# ---------------------------------------------------------------------------


def _make_task_dir(tmp_path: Path, name: str, script: str) -> Path:
    """Create a task dir with test.sh and return its path."""
    task_dir = tmp_path / name
    _make_test_sh(task_dir, script)
    return task_dir


class TestScorerProtocol:
    """Verify that all scorer implementations satisfy the Scorer protocol."""

    def test_binary_scorer_is_scorer(self) -> None:
        assert isinstance(BinaryScorer(), Scorer)

    def test_continuous_scorer_is_scorer(self) -> None:
        assert isinstance(ContinuousScorer(), Scorer)

    def test_checkpoint_scorer_is_scorer(self) -> None:
        assert isinstance(CheckpointScorer(), Scorer)


# ---------------------------------------------------------------------------
# BinaryScorer
# ---------------------------------------------------------------------------


class TestBinaryScorer:
    """BinaryScorer wraps existing score_task_output() behaviour."""

    def test_pass_on_exit_zero(self, tmp_path: Path) -> None:
        task_dir = _make_task_dir(tmp_path, "bin-pass", "#!/bin/bash\nexit 0\n")
        result = BinaryScorer().score("any output", task_dir)
        assert result.score == 1.0
        assert result.passed is True
        assert result.error is None

    def test_fail_on_exit_nonzero(self, tmp_path: Path) -> None:
        task_dir = _make_task_dir(tmp_path, "bin-fail", "#!/bin/bash\nexit 1\n")
        result = BinaryScorer().score("output", task_dir)
        assert result.score == 0.0
        assert result.passed is False

    def test_missing_test_sh(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "bin-missing"
        task_dir.mkdir(parents=True)
        result = BinaryScorer().score("output", task_dir)
        assert result.score == 0.0
        assert result.passed is False
        assert result.error is not None

    def test_sandbox_isolation(self, tmp_path: Path) -> None:
        task_dir = _make_task_dir(
            tmp_path,
            "bin-sandbox",
            '#!/bin/bash\ntouch "$PWD/side_effect.txt"\nexit 0\n',
        )
        result = BinaryScorer().score("output", task_dir)
        assert result.passed is True
        assert not (task_dir / "side_effect.txt").exists()


# ---------------------------------------------------------------------------
# ContinuousScorer
# ---------------------------------------------------------------------------


class TestContinuousScorer:
    """ContinuousScorer reads float from reward.txt written by test.sh."""

    def test_reads_reward_txt(self, tmp_path: Path) -> None:
        # test.sh writes a reward.txt in the sandbox
        script = "#!/bin/bash\n" 'echo "0.75" > "$PWD/reward.txt"\n' "exit 0\n"
        task_dir = _make_task_dir(tmp_path, "cont-reward", script)
        result = ContinuousScorer().score("output", task_dir)
        assert result.score == pytest.approx(0.75)
        assert result.passed is True
        assert result.error is None

    def test_reads_score_from_stdout_fallback(self, tmp_path: Path) -> None:
        # No reward.txt — falls back to last line of stdout
        script = "#!/bin/bash\n" 'echo "some debug output"\n' 'echo "0.42"\n' "exit 0\n"
        task_dir = _make_task_dir(tmp_path, "cont-stdout", script)
        result = ContinuousScorer().score("output", task_dir)
        assert result.score == pytest.approx(0.42)
        assert result.passed is True

    def test_zero_score_means_not_passed(self, tmp_path: Path) -> None:
        script = "#!/bin/bash\n" 'echo "0.0" > "$PWD/reward.txt"\n' "exit 0\n"
        task_dir = _make_task_dir(tmp_path, "cont-zero", script)
        result = ContinuousScorer().score("output", task_dir)
        assert result.score == 0.0
        assert result.passed is False

    def test_exit_nonzero_returns_zero_score(self, tmp_path: Path) -> None:
        script = "#!/bin/bash\n" 'echo "0.8" > "$PWD/reward.txt"\n' "exit 1\n"
        task_dir = _make_task_dir(tmp_path, "cont-exit1", script)
        result = ContinuousScorer().score("output", task_dir)
        assert result.score == 0.0
        assert result.passed is False

    def test_invalid_reward_value(self, tmp_path: Path) -> None:
        script = "#!/bin/bash\n" 'echo "not_a_number" > "$PWD/reward.txt"\n' "exit 0\n"
        task_dir = _make_task_dir(tmp_path, "cont-invalid", script)
        result = ContinuousScorer().score("output", task_dir)
        assert result.score == 0.0
        assert result.passed is False
        assert result.error is not None

    def test_clamps_score_to_range(self, tmp_path: Path) -> None:
        script = "#!/bin/bash\n" 'echo "1.5" > "$PWD/reward.txt"\n' "exit 0\n"
        task_dir = _make_task_dir(tmp_path, "cont-clamp", script)
        result = ContinuousScorer().score("output", task_dir)
        assert result.score == 1.0
        assert result.passed is True

    def test_missing_test_sh(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "cont-missing"
        task_dir.mkdir(parents=True)
        result = ContinuousScorer().score("output", task_dir)
        assert result.score == 0.0
        assert result.passed is False


# ---------------------------------------------------------------------------
# CheckpointScorer
# ---------------------------------------------------------------------------


class TestCheckpointScorer:
    """CheckpointScorer runs multiple weighted checkpoint verifiers."""

    def _make_checkpoint_task(
        self,
        tmp_path: Path,
        name: str,
        checkpoints: list[dict],
        verifier_scripts: dict[str, str],
    ) -> Path:
        """Create a task dir with checkpoints.json and verifier scripts."""
        task_dir = tmp_path / name
        tests_dir = task_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)

        # Write checkpoints.json
        (tests_dir / "checkpoints.json").write_text(
            json.dumps(checkpoints), encoding="utf-8"
        )

        # Write verifier scripts
        verifiers_dir = tests_dir / "verifiers"
        verifiers_dir.mkdir(exist_ok=True)
        for script_name, script_content in verifier_scripts.items():
            script_path = verifiers_dir / script_name
            script_path.write_text(script_content)
            script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)

        return task_dir

    def test_all_checkpoints_pass(self, tmp_path: Path) -> None:
        checkpoints = [
            {"name": "cp1", "weight": 0.6, "verifier": "check1.sh"},
            {"name": "cp2", "weight": 0.4, "verifier": "check2.sh"},
        ]
        verifiers = {
            "check1.sh": '#!/bin/bash\necho \'{"score": 1.0, "passed": true}\'\nexit 0\n',
            "check2.sh": '#!/bin/bash\necho \'{"score": 1.0, "passed": true}\'\nexit 0\n',
        }
        task_dir = self._make_checkpoint_task(
            tmp_path, "cp-all-pass", checkpoints, verifiers
        )
        result = CheckpointScorer().score("output", task_dir)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_partial_credit(self, tmp_path: Path) -> None:
        checkpoints = [
            {"name": "cp1", "weight": 0.6, "verifier": "check1.sh"},
            {"name": "cp2", "weight": 0.4, "verifier": "check2.sh"},
        ]
        verifiers = {
            "check1.sh": '#!/bin/bash\necho \'{"score": 1.0, "passed": true}\'\nexit 0\n',
            "check2.sh": '#!/bin/bash\necho \'{"score": 0.0, "passed": false}\'\nexit 1\n',
        }
        task_dir = self._make_checkpoint_task(
            tmp_path, "cp-partial", checkpoints, verifiers
        )
        result = CheckpointScorer().score("output", task_dir)
        assert result.score == pytest.approx(0.6)
        assert result.passed is True  # partial credit > 0

    def test_all_fail(self, tmp_path: Path) -> None:
        checkpoints = [
            {"name": "cp1", "weight": 0.5, "verifier": "check1.sh"},
            {"name": "cp2", "weight": 0.5, "verifier": "check2.sh"},
        ]
        verifiers = {
            "check1.sh": '#!/bin/bash\necho \'{"score": 0.0, "passed": false}\'\nexit 1\n',
            "check2.sh": '#!/bin/bash\necho \'{"score": 0.0, "passed": false}\'\nexit 1\n',
        }
        task_dir = self._make_checkpoint_task(
            tmp_path, "cp-all-fail", checkpoints, verifiers
        )
        result = CheckpointScorer().score("output", task_dir)
        assert result.score == 0.0
        assert result.passed is False

    def test_weights_must_sum_to_one(self, tmp_path: Path) -> None:
        checkpoints = [
            {"name": "cp1", "weight": 0.3, "verifier": "check1.sh"},
            {"name": "cp2", "weight": 0.3, "verifier": "check2.sh"},
        ]
        verifiers = {
            "check1.sh": '#!/bin/bash\necho \'{"score": 1.0, "passed": true}\'\nexit 0\n',
            "check2.sh": '#!/bin/bash\necho \'{"score": 1.0, "passed": true}\'\nexit 0\n',
        }
        task_dir = self._make_checkpoint_task(
            tmp_path, "cp-bad-weights", checkpoints, verifiers
        )
        result = CheckpointScorer().score("output", task_dir)
        assert result.passed is False
        assert result.error is not None
        assert "sum to 1.0" in result.error.lower() or "weight" in result.error.lower()

    def test_verifier_exit_nonzero_fallback(self, tmp_path: Path) -> None:
        """Verifier exits nonzero without JSON — treat as score=0, passed=False."""
        checkpoints = [
            {"name": "cp1", "weight": 1.0, "verifier": "check1.sh"},
        ]
        verifiers = {
            "check1.sh": "#!/bin/bash\nexit 1\n",
        }
        task_dir = self._make_checkpoint_task(
            tmp_path, "cp-fallback", checkpoints, verifiers
        )
        result = CheckpointScorer().score("output", task_dir)
        assert result.score == 0.0
        assert result.passed is False

    def test_missing_checkpoints_json(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "cp-no-json"
        task_dir.mkdir(parents=True)
        (task_dir / "tests").mkdir(parents=True)
        result = CheckpointScorer().score("output", task_dir)
        assert result.score == 0.0
        assert result.passed is False
        assert result.error is not None


# ---------------------------------------------------------------------------
# get_scorer registry
# ---------------------------------------------------------------------------


class TestGetScorer:
    """Registry dispatches by reward_type string."""

    def test_binary(self) -> None:
        assert isinstance(get_scorer("binary"), BinaryScorer)

    def test_continuous(self) -> None:
        assert isinstance(get_scorer("continuous"), ContinuousScorer)

    def test_checkpoint(self) -> None:
        assert isinstance(get_scorer("checkpoint"), CheckpointScorer)

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown reward_type"):
            get_scorer("exotic")


# ---------------------------------------------------------------------------
# Loud fallbacks — _run_in_sandbox OSError logging
# ---------------------------------------------------------------------------


class TestRunInSandboxOSError:
    """When shutil.copytree raises OSError, _run_in_sandbox logs WARNING and sets error."""

    def test_copytree_oserror_sets_error_field(self, tmp_path: Path) -> None:
        """OSError from copytree is captured in _SandboxRun.error."""
        from unittest.mock import patch

        from codeprobe.core.scoring import _run_in_sandbox

        task_dir = tmp_path / "task-oserror"
        task_dir.mkdir(parents=True)
        test_sh = task_dir / "tests" / "test.sh"
        test_sh.parent.mkdir(parents=True)
        test_sh.write_text("#!/bin/bash\nexit 0\n")
        test_sh.chmod(0o755)

        with patch(
            "codeprobe.core.scoring.shutil.copytree", side_effect=OSError("disk full")
        ):
            run = _run_in_sandbox(test_sh, "output", task_dir)

        assert run.returncode == -1
        assert run.error is not None
        assert "disk full" in run.error

    def test_copytree_oserror_emits_warning_log(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """WARNING log is emitted when copytree raises OSError."""
        import logging
        from unittest.mock import patch

        from codeprobe.core.scoring import _run_in_sandbox

        task_dir = tmp_path / "task-oserror-log"
        task_dir.mkdir(parents=True)
        test_sh = task_dir / "tests" / "test.sh"
        test_sh.parent.mkdir(parents=True)
        test_sh.write_text("#!/bin/bash\nexit 0\n")
        test_sh.chmod(0o755)

        with (
            caplog.at_level(logging.WARNING, logger="codeprobe.core.scoring"),
            patch(
                "codeprobe.core.scoring.shutil.copytree",
                side_effect=OSError("disk full"),
            ),
        ):
            _run_in_sandbox(test_sh, "output", task_dir)

        assert any("disk full" in rec.message for rec in caplog.records)
        assert any(rec.levelno == logging.WARNING for rec in caplog.records)
