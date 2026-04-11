"""Tests for DualScorer — composes direct + artifact scorers."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from codeprobe.core.scoring import (
    VALID_REWARD_TYPES,
    ArtifactScorer,
    BinaryScorer,
    ContinuousScorer,
    DualScorer,
    ScoreResult,
    get_scorer,
)

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


def _write_metadata(
    task_dir: Path,
    *,
    reward_type: str = "binary",
    scoring_policy: str = "",
    weight_direct: float = 0.5,
    weight_artifact: float = 0.5,
) -> None:
    """Write a minimal ``metadata.json`` so DualScorer can read verification."""
    metadata = {
        "verification": {
            "verification_mode": "dual",
            "reward_type": reward_type,
            "scoring_policy": scoring_policy,
            "weight_direct": weight_direct,
            "weight_artifact": weight_artifact,
        }
    }
    (task_dir / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")


def _write_test_sh(task_dir: Path, exit_code: int) -> None:
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    script = tests_dir / "test.sh"
    script.write_text(f"#!/usr/bin/env bash\nexit {exit_code}\n", encoding="utf-8")
    st = os.stat(script)
    os.chmod(script, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_ground_truth(
    task_dir: Path,
    *,
    answer_type: str = "boolean",
    answer: object = True,
) -> None:
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "ground_truth.json").write_text(
        json.dumps({"answer_type": answer_type, "answer": answer}),
        encoding="utf-8",
    )


def _write_answer(
    task_dir: Path,
    *,
    answer: object,
) -> None:
    (task_dir / "answer.json").write_text(
        json.dumps({"answer": answer}),
        encoding="utf-8",
    )


@pytest.fixture
def passing_task_dir(tmp_path: Path) -> Path:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_metadata(task_dir)  # default: binary, policy=""
    _write_test_sh(task_dir, exit_code=0)
    _write_ground_truth(task_dir, answer_type="boolean", answer=True)
    _write_answer(task_dir, answer=True)
    return task_dir


@pytest.fixture
def failing_direct_passing_artifact(tmp_path: Path) -> Path:
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_metadata(task_dir)  # default: binary, policy=""
    _write_test_sh(task_dir, exit_code=1)  # direct fails
    _write_ground_truth(task_dir, answer_type="boolean", answer=True)
    _write_answer(task_dir, answer=True)  # artifact passes
    return task_dir


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_dual_in_valid_reward_types():
    assert "dual" in VALID_REWARD_TYPES


def test_get_scorer_dual_returns_dual_scorer_instance():
    scorer = get_scorer("dual")
    assert isinstance(scorer, DualScorer)


def test_dual_scorer_no_arg_constructor():
    # Must construct with no arguments so the registry can instantiate it
    scorer = DualScorer()
    assert isinstance(scorer, DualScorer)


# ---------------------------------------------------------------------------
# Composition happy path
# ---------------------------------------------------------------------------


def test_default_policy_returns_score_direct(passing_task_dir: Path):
    # default fixture metadata: policy=""
    result = DualScorer().score("", passing_task_dir)
    assert result.score == 1.0  # == score_direct
    assert result.details["score_direct"] == 1.0
    assert result.details["score_artifact"] == 1.0
    assert result.details["passed_direct"] is True
    assert result.details["passed_artifact"] is True
    assert result.details["scoring_policy"] == ""


def test_details_dict_contains_required_keys(passing_task_dir: Path):
    result = DualScorer().score("", passing_task_dir)
    for key in (
        "score_direct",
        "score_artifact",
        "passed_direct",
        "passed_artifact",
        "scoring_policy",
    ):
        assert key in result.details


def test_default_policy_score_equals_direct_when_artifact_differs(
    failing_direct_passing_artifact: Path,
):
    # default fixture metadata: policy=""
    result = DualScorer().score("", failing_direct_passing_artifact)
    # direct = 0.0 (test.sh exit 1), artifact = 1.0 (match)
    assert result.details["score_direct"] == 0.0
    assert result.details["score_artifact"] == 1.0
    assert result.score == 0.0  # default policy = score_direct


# ---------------------------------------------------------------------------
# scoring_policy values
# ---------------------------------------------------------------------------


def test_policy_min(failing_direct_passing_artifact: Path):
    _write_metadata(failing_direct_passing_artifact, scoring_policy="min")
    result = DualScorer().score("", failing_direct_passing_artifact)
    assert result.details["score_direct"] == 0.0
    assert result.details["score_artifact"] == 1.0
    assert result.score == 0.0  # min(0.0, 1.0)
    assert result.details["scoring_policy"] == "min"


def test_policy_mean(failing_direct_passing_artifact: Path):
    _write_metadata(failing_direct_passing_artifact, scoring_policy="mean")
    result = DualScorer().score("", failing_direct_passing_artifact)
    assert result.score == pytest.approx(0.5)  # (0.0 + 1.0) / 2
    assert result.details["scoring_policy"] == "mean"


def test_policy_weighted(failing_direct_passing_artifact: Path):
    _write_metadata(
        failing_direct_passing_artifact,
        scoring_policy="weighted",
        weight_direct=0.3,
        weight_artifact=0.7,
    )
    result = DualScorer().score("", failing_direct_passing_artifact)
    # 0.3 * 0.0 + 0.7 * 1.0 == 0.7
    assert result.score == pytest.approx(0.7)
    assert result.details["scoring_policy"] == "weighted"


def test_policy_weighted_balanced(passing_task_dir: Path):
    _write_metadata(
        passing_task_dir,
        scoring_policy="weighted",
        weight_direct=0.4,
        weight_artifact=0.6,
    )
    result = DualScorer().score("", passing_task_dir)
    # Both 1.0: 0.4 + 0.6 = 1.0
    assert result.score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Graceful degradation
# ---------------------------------------------------------------------------


def test_missing_answer_json_artifact_leg_fails_direct_runs(tmp_path: Path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_metadata(task_dir)
    _write_test_sh(task_dir, exit_code=0)  # direct passes
    _write_ground_truth(task_dir, answer_type="boolean", answer=True)
    # No answer.json written

    result = DualScorer().score("", task_dir)

    assert result.details["score_direct"] == 1.0
    assert result.details["passed_direct"] is True
    assert result.details["score_artifact"] == 0.0
    assert result.details["passed_artifact"] is False
    assert "error_artifact" in result.details
    assert result.details["error_artifact"]  # non-empty


def test_missing_test_sh_direct_leg_fails_artifact_runs(tmp_path: Path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_metadata(task_dir)
    # No test.sh written
    _write_ground_truth(task_dir, answer_type="boolean", answer=True)
    _write_answer(task_dir, answer=True)

    result = DualScorer().score("", task_dir)

    assert result.details["score_direct"] == 0.0
    assert result.details["passed_direct"] is False
    assert "error_direct" in result.details
    assert result.details["error_direct"]

    assert result.details["score_artifact"] == 1.0
    assert result.details["passed_artifact"] is True


def test_both_legs_fail_gracefully(tmp_path: Path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    _write_metadata(task_dir, scoring_policy="mean")
    # Nothing else written — no test.sh, no ground_truth.json, no answer.json

    result = DualScorer().score("", task_dir)

    assert result.details["score_direct"] == 0.0
    assert result.details["score_artifact"] == 0.0
    assert result.details["passed_direct"] is False
    assert result.details["passed_artifact"] is False
    assert "error_direct" in result.details
    assert "error_artifact" in result.details
    assert result.score == 0.0
    assert result.passed is False
    assert result.error is not None


# ---------------------------------------------------------------------------
# Both legs must run even when sub-scorer raises
# ---------------------------------------------------------------------------


def test_both_legs_run_when_direct_raises(
    monkeypatch: pytest.MonkeyPatch,
    passing_task_dir: Path,
):
    def _boom(self, agent_output, task_dir):
        raise RuntimeError("direct exploded")

    monkeypatch.setattr(BinaryScorer, "score", _boom)

    result = DualScorer().score("", passing_task_dir)

    # Direct leg captured the exception
    assert result.details["score_direct"] == 0.0
    assert result.details["passed_direct"] is False
    assert "error_direct" in result.details
    assert "direct exploded" in result.details["error_direct"]

    # Artifact leg STILL ran
    assert result.details["score_artifact"] == 1.0
    assert result.details["passed_artifact"] is True


def test_both_legs_run_when_artifact_raises(
    monkeypatch: pytest.MonkeyPatch,
    passing_task_dir: Path,
):
    def _boom(self, agent_output, task_dir):
        raise RuntimeError("artifact exploded")

    monkeypatch.setattr(ArtifactScorer, "score", _boom)

    result = DualScorer().score("", passing_task_dir)

    assert result.details["score_artifact"] == 0.0
    assert result.details["passed_artifact"] is False
    assert "error_artifact" in result.details
    assert "artifact exploded" in result.details["error_artifact"]

    # Direct leg STILL ran
    assert result.details["score_direct"] == 1.0
    assert result.details["passed_direct"] is True


def test_both_legs_run_when_both_raise(
    monkeypatch: pytest.MonkeyPatch,
    passing_task_dir: Path,
):
    def _boom_direct(self, agent_output, task_dir):
        raise RuntimeError("direct fail")

    def _boom_artifact(self, agent_output, task_dir):
        raise RuntimeError("artifact fail")

    monkeypatch.setattr(BinaryScorer, "score", _boom_direct)
    monkeypatch.setattr(ArtifactScorer, "score", _boom_artifact)

    _write_metadata(passing_task_dir, scoring_policy="mean")
    result = DualScorer().score("", passing_task_dir)

    assert result.details["score_direct"] == 0.0
    assert result.details["score_artifact"] == 0.0
    assert "error_direct" in result.details
    assert "error_artifact" in result.details
    assert result.score == 0.0


# ---------------------------------------------------------------------------
# reward_type selects the direct scorer
# ---------------------------------------------------------------------------


def test_reward_type_continuous_uses_continuous_scorer(
    monkeypatch: pytest.MonkeyPatch,
    passing_task_dir: Path,
):
    called = {"binary": 0, "continuous": 0}

    orig_binary = BinaryScorer.score
    orig_continuous = ContinuousScorer.score

    def _wrap_binary(self, agent_output, task_dir):
        called["binary"] += 1
        return orig_binary(self, agent_output, task_dir)

    def _wrap_continuous(self, agent_output, task_dir):
        called["continuous"] += 1
        return orig_continuous(self, agent_output, task_dir)

    monkeypatch.setattr(BinaryScorer, "score", _wrap_binary)
    monkeypatch.setattr(ContinuousScorer, "score", _wrap_continuous)

    _write_metadata(passing_task_dir, reward_type="continuous")
    DualScorer().score("", passing_task_dir)

    assert called["continuous"] == 1
    assert called["binary"] == 0


def test_reward_type_binary_uses_binary_scorer(
    monkeypatch: pytest.MonkeyPatch,
    passing_task_dir: Path,
):
    called = {"binary": 0, "continuous": 0}

    orig_binary = BinaryScorer.score
    orig_continuous = ContinuousScorer.score

    def _wrap_binary(self, agent_output, task_dir):
        called["binary"] += 1
        return orig_binary(self, agent_output, task_dir)

    def _wrap_continuous(self, agent_output, task_dir):
        called["continuous"] += 1
        return orig_continuous(self, agent_output, task_dir)

    monkeypatch.setattr(BinaryScorer, "score", _wrap_binary)
    monkeypatch.setattr(ContinuousScorer, "score", _wrap_continuous)

    _write_metadata(passing_task_dir, reward_type="binary")
    DualScorer().score("", passing_task_dir)

    assert called["binary"] == 1
    assert called["continuous"] == 0


# ---------------------------------------------------------------------------
# ScoreResult shape
# ---------------------------------------------------------------------------


def test_result_is_score_result(passing_task_dir: Path):
    result = DualScorer().score("", passing_task_dir)
    assert isinstance(result, ScoreResult)


# ---------------------------------------------------------------------------
# Strict metadata validation — missing/unparseable metadata fails explicitly
# ---------------------------------------------------------------------------


def test_missing_metadata_json_fails_with_error(tmp_path: Path):
    """A dual task with no metadata.json must fail explicitly, not silently default."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    # No metadata.json at all — DualScorer must not silently proceed
    _write_test_sh(task_dir, exit_code=0)
    _write_ground_truth(task_dir, answer_type="boolean", answer=True)
    _write_answer(task_dir, answer=True)

    result = DualScorer().score("", task_dir)

    assert result.score == 0.0
    assert result.passed is False
    assert result.error is not None
    assert "metadata" in result.error.lower()


def test_unparseable_metadata_json_fails_with_error(tmp_path: Path):
    """A dual task with malformed metadata.json must fail explicitly."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "metadata.json").write_text("{not valid json", encoding="utf-8")
    _write_test_sh(task_dir, exit_code=0)
    _write_ground_truth(task_dir, answer_type="boolean", answer=True)
    _write_answer(task_dir, answer=True)

    result = DualScorer().score("", task_dir)

    assert result.score == 0.0
    assert result.passed is False
    assert result.error is not None
    assert "metadata" in result.error.lower()


def test_metadata_missing_verification_block_fails(tmp_path: Path):
    """metadata.json exists but has no 'verification' key — must fail."""
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "metadata.json").write_text(
        json.dumps({"name": "test-task"}), encoding="utf-8"
    )
    _write_test_sh(task_dir, exit_code=0)
    _write_ground_truth(task_dir, answer_type="boolean", answer=True)
    _write_answer(task_dir, answer=True)

    result = DualScorer().score("", task_dir)

    assert result.score == 0.0
    assert result.passed is False
    assert result.error is not None
    assert "metadata" in result.error.lower()
