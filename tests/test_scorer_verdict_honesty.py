"""Typed verdict coverage for continuous and composite scorer families."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from codeprobe.analysis.validity import TrialClass, classify_trial
from codeprobe.core.scoring import (
    CheckpointScorer,
    ContinuousScorer,
    OracleChecksScorer,
    ScoreResult,
)
from codeprobe.mining.org_scale_oracle import oracle_check
from codeprobe.models.experiment import CompletedTask


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _assert_infra(result: ScoreResult) -> None:
    trial = CompletedTask(
        task_id="verdict-honesty",
        automated_score=result.score,
        status="completed",
        verdict=result.verdict,
        scoring_details={"passed": result.passed},
    )
    assert result.verdict == "verifier_error"
    assert classify_trial(trial) is TrialClass.INFRA_FAILURE


def test_continuous_invalid_reward_is_typed_infra(tmp_path: Path) -> None:
    task_dir = tmp_path / "continuous"
    _write_executable(
        task_dir / "tests" / "test.sh",
        "#!/bin/sh\nprintf 'not-a-score\\n' > \"$PWD/reward.txt\"\n",
    )

    _assert_infra(ContinuousScorer().score("", task_dir))


def test_checkpoint_invalid_json_is_typed_infra(tmp_path: Path) -> None:
    task_dir = tmp_path / "checkpoint"
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "checkpoints.json").write_text(
        json.dumps([{"name": "broken", "weight": 1.0, "verifier": "broken.sh"}]),
        encoding="utf-8",
    )
    _write_executable(
        tests_dir / "verifiers" / "broken.sh",
        "#!/bin/sh\nprintf 'not-json\\n'\n",
    )

    _assert_infra(CheckpointScorer().score("", task_dir))


def test_oracle_checks_invalid_rubric_is_typed_infra(tmp_path: Path) -> None:
    task_dir = tmp_path / "oracle-checks"
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "rubric.json").write_text("{broken", encoding="utf-8")

    _assert_infra(OracleChecksScorer().score("", task_dir))


@pytest.mark.parametrize(
    ("scorer", "manifest_name", "manifest"),
    [
        (
            CheckpointScorer(),
            "checkpoints.json",
            [{"name": "answer", "weight": 1.0, "verifier": "answer.sh"}],
        ),
        (
            OracleChecksScorer(),
            "rubric.json",
            [{"name": "answer", "weight": 1.0, "verifier": "answer.sh"}],
        ),
    ],
)
def test_composite_valid_results_are_agent_attributable(
    tmp_path: Path,
    scorer: CheckpointScorer | OracleChecksScorer,
    manifest_name: str,
    manifest: list[dict[str, object]],
) -> None:
    task_dir = tmp_path / manifest_name
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / manifest_name).write_text(json.dumps(manifest), encoding="utf-8")
    _write_executable(
        tests_dir / "verifiers" / "answer.sh",
        "#!/bin/sh\nprintf '{\"score\": 0.0, \"passed\": false}\\n'\n",
    )

    result = scorer.score("", task_dir)

    assert result.verdict == "incorrect"


def test_unknown_org_scale_oracle_type_is_typed_verifier_error(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "unknown-oracle"
    task_dir.mkdir()
    (task_dir / "ground_truth.json").write_text(
        json.dumps({"oracle_type": "unknown", "expected": "value"}),
        encoding="utf-8",
    )
    (task_dir / "answer.txt").write_text("value\n", encoding="utf-8")

    result = oracle_check(task_dir)

    assert result["verdict"] == "verifier_error"
    assert result["error"] == "Unknown oracle_type: 'unknown'"
