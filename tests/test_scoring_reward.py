"""Reward vs IR diagnostics — codeprobe-voxa.

The IR scorers (``score_file_list`` / ``score_symbol_list`` / legacy
file-list / ContinuousScorer over an oracle ``metrics.json``) report
reward as **oracle-matching** (recall, or ``weighted_recall`` when the
oracle uses tier weights). Precision and F1 are computed alongside but
live in ``ir_metrics`` so over-shipping no longer drags the reward down.

These tests pin the contract for the cases called out in the bead:

* exact match (recall = precision = 1.0)
* pure-recall match (recall = 1.0, precision low — over-ship extreme)
* over-ship modest (recall = 1.0, precision moderate)
* under-ship (recall < 1.0, precision = 1.0)
* ContinuousScorer pivots reward off ``reward.txt = f1`` to the recall
  it reads from ``metrics.json``
* ContinuousScorer prefers ``weighted_recall`` over ``recall`` when both
  are present
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from codeprobe.core.scoring import (
    ContinuousScorer,
    score_file_list,
    score_symbol_list,
)


# ---------------------------------------------------------------------------
# score_file_list — bead acceptance cases
# ---------------------------------------------------------------------------


class TestScoreFileListReward:
    def test_exact_match_reward_is_one(self) -> None:
        result = score_file_list(["a.py", "b.py"], ["a.py", "b.py"])
        assert result.score == pytest.approx(1.0)
        assert result.reward_score == pytest.approx(1.0)
        assert result.ir_metrics == {
            "precision": pytest.approx(1.0),
            "recall": pytest.approx(1.0),
            "f1": pytest.approx(1.0),
        }
        assert result.passed is True

    def test_pure_recall_extreme_overship_keeps_full_reward(self) -> None:
        """Agent dumps 100 files including all 2 expected → recall=1.0.

        Reward should be 1.0 (it found everything). Precision = 0.02 and
        F1 ≈ 0.04 stay in ir_metrics so reviewers can see the noisy answer.
        """
        expected = ["a.py", "b.py"]
        actual = expected + [f"noise_{i}.py" for i in range(98)]
        result = score_file_list(expected, actual)
        assert result.score == pytest.approx(1.0)
        assert result.reward_score == pytest.approx(1.0)
        assert result.ir_metrics["recall"] == pytest.approx(1.0)
        assert result.ir_metrics["precision"] == pytest.approx(0.02)
        assert result.ir_metrics["f1"] == pytest.approx(0.0392, abs=1e-3)
        assert result.passed is True

    def test_modest_overship_keeps_full_reward(self) -> None:
        """Two extra files on top of the two expected → recall still 1.0."""
        result = score_file_list(["a.py", "b.py"], ["a.py", "b.py", "c.py", "d.py"])
        assert result.score == pytest.approx(1.0)
        assert result.reward_score == pytest.approx(1.0)
        assert result.ir_metrics["recall"] == pytest.approx(1.0)
        assert result.ir_metrics["precision"] == pytest.approx(0.5)
        assert result.ir_metrics["f1"] == pytest.approx(2 / 3)

    def test_undership_drops_reward(self) -> None:
        """Agent finds only half the oracle → reward = 0.5."""
        result = score_file_list(
            ["a.py", "b.py", "c.py", "d.py"],
            ["a.py", "b.py"],
        )
        assert result.score == pytest.approx(0.5)
        assert result.reward_score == pytest.approx(0.5)
        assert result.ir_metrics["recall"] == pytest.approx(0.5)
        assert result.ir_metrics["precision"] == pytest.approx(1.0)
        assert result.ir_metrics["f1"] == pytest.approx(2 / 3)

    def test_no_overlap_zero_reward(self) -> None:
        result = score_file_list(["a.py"], ["z.py"])
        assert result.score == pytest.approx(0.0)
        assert result.reward_score == pytest.approx(0.0)
        assert result.ir_metrics["recall"] == pytest.approx(0.0)
        assert result.passed is False

    def test_empty_expected_returns_zero(self) -> None:
        result = score_file_list([], ["a.py"])
        # No oracle to match → recall undefined → zero, no IR data.
        assert result.score == pytest.approx(0.0)
        assert result.reward_score == pytest.approx(0.0)
        assert result.ir_metrics == {
            "precision": pytest.approx(0.0),
            "recall": pytest.approx(0.0),
            "f1": pytest.approx(0.0),
        }


# ---------------------------------------------------------------------------
# score_symbol_list — same shape over normalized symbol names
# ---------------------------------------------------------------------------


class TestScoreSymbolListReward:
    def test_exact_match_reward_is_one(self) -> None:
        result = score_symbol_list(["Foo", "Bar"], ["Foo", "Bar"])
        assert result.score == pytest.approx(1.0)
        assert result.reward_score == pytest.approx(1.0)
        assert result.ir_metrics["recall"] == pytest.approx(1.0)
        assert result.ir_metrics["precision"] == pytest.approx(1.0)

    def test_overship_keeps_full_reward(self) -> None:
        result = score_symbol_list(["Foo"], ["Foo", "Bar", "Baz"])
        assert result.score == pytest.approx(1.0)
        assert result.reward_score == pytest.approx(1.0)
        assert result.ir_metrics["recall"] == pytest.approx(1.0)
        assert result.ir_metrics["precision"] == pytest.approx(1 / 3)

    def test_undership_drops_reward(self) -> None:
        result = score_symbol_list(["Foo", "Bar"], ["Foo"])
        assert result.score == pytest.approx(0.5)
        assert result.reward_score == pytest.approx(0.5)
        assert result.ir_metrics["recall"] == pytest.approx(0.5)
        assert result.ir_metrics["precision"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# ContinuousScorer — reward derivation from oracle metrics.json
# ---------------------------------------------------------------------------


def _make_oracle_task(tmp_path: Path, name: str, script: str) -> Path:
    task_dir = tmp_path / name
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_sh = tests_dir / "test.sh"
    test_sh.write_text(script, encoding="utf-8")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return task_dir


class TestContinuousScorerReward:
    def test_extreme_overship_reward_is_recall_not_f1(self, tmp_path: Path) -> None:
        """Mirrors the e5d7a4e7 case from the codeprobe-wo7n run.

        Oracle wrote reward.txt = 0.4092 (F1) and metrics.json with
        recall = 1.0 / precision = 0.2571. Reward should pivot to 1.0
        (the recall) so over-shipping doesn't fake a quality gap.
        """
        script = (
            "#!/bin/bash\n"
            'echo "0.4092" > "$PWD/reward.txt"\n'
            'cat > "$PWD/metrics.json" <<\'JSON\'\n'
            '{"score": 0.4092, "metric": "f1", "f1": 0.4092, '
            '"precision": 0.2571, "recall": 1.0, '
            '"matched": 80, "expected_count": 80, '
            '"agent_files_count": 311, "weighted_recall": null}\n'
            "JSON\n"
            "exit 0\n"
        )
        task_dir = _make_oracle_task(tmp_path, "extreme-overship", script)
        result = ContinuousScorer().score("output", task_dir)
        assert result.score == pytest.approx(1.0)
        assert result.reward_score == pytest.approx(1.0)
        assert result.ir_metrics["precision"] == pytest.approx(0.2571)
        assert result.ir_metrics["recall"] == pytest.approx(1.0)
        assert result.ir_metrics["f1"] == pytest.approx(0.4092)
        assert result.passed is True

    def test_weighted_recall_takes_precedence(self, tmp_path: Path) -> None:
        """Org-scale weighted oracle: reward = weighted_recall, not raw recall."""
        script = (
            "#!/bin/bash\n"
            'echo "0.55" > "$PWD/reward.txt"\n'
            'cat > "$PWD/metrics.json" <<\'JSON\'\n'
            '{"score": 0.55, "metric": "weighted_f1", "f1": 0.55, '
            '"precision": 0.6, "recall": 0.7, '
            '"matched": 7, "expected_count": 10, '
            '"agent_files_count": 12, "weighted_recall": 0.85}\n'
            "JSON\n"
            "exit 0\n"
        )
        task_dir = _make_oracle_task(tmp_path, "weighted", script)
        result = ContinuousScorer().score("output", task_dir)
        assert result.score == pytest.approx(0.85)
        assert result.reward_score == pytest.approx(0.85)
        assert result.ir_metrics["weighted_recall"] == pytest.approx(0.85)
        assert result.ir_metrics["recall"] == pytest.approx(0.7)
        assert result.ir_metrics["precision"] == pytest.approx(0.6)
        assert result.ir_metrics["f1"] == pytest.approx(0.55)

    def test_legacy_oracle_without_recall_falls_back_to_reward_txt(
        self, tmp_path: Path
    ) -> None:
        """No ``recall`` in metrics.json → score stays whatever reward.txt says.

        Older mined tasks may emit a continuous score without IR metadata
        (custom test.sh scripts). We must not break those.
        """
        script = (
            "#!/bin/bash\n"
            'echo "0.5" > "$PWD/reward.txt"\n'
            "exit 0\n"
        )
        task_dir = _make_oracle_task(tmp_path, "legacy", script)
        result = ContinuousScorer().score("output", task_dir)
        assert result.score == pytest.approx(0.5)
        assert result.reward_score == pytest.approx(0.5)
        # No IR metrics surfaced when the oracle didn't emit them.
        assert result.ir_metrics == {}


# ---------------------------------------------------------------------------
# Aggregate schema — mean_reward + ir_diagnostics block
# ---------------------------------------------------------------------------


def test_aggregate_emits_mean_reward_and_ir_diagnostics(tmp_path: Path) -> None:
    """``experiment_aggregate`` must surface mean_reward as the headline and
    nest mean_precision / mean_recall / mean_f1 under ir_diagnostics. The
    flat top-level fields stay populated for back-compat with older tooling.
    """
    from click.testing import CliRunner

    from codeprobe.cli import main
    from codeprobe.core.experiment import (
        create_experiment_dir,
        save_config_results,
    )
    from codeprobe.models.experiment import (
        CompletedTask,
        Experiment,
        ExperimentConfig,
    )

    exp = Experiment(
        name="reward-schema",
        configs=[ExperimentConfig(label="baseline")],
    )
    exp_dir = create_experiment_dir(tmp_path, exp)

    completed = [
        CompletedTask(
            task_id="t1",
            automated_score=1.0,
            duration_seconds=1.0,
            cost_usd=0.01,
            scoring_details={
                "passed": True,
                "precision": 0.25,
                "recall": 1.0,
                "f1": 0.40,
            },
        ),
        CompletedTask(
            task_id="t2",
            automated_score=0.5,
            duration_seconds=1.0,
            cost_usd=0.01,
            scoring_details={
                "passed": True,
                "precision": 1.0,
                "recall": 0.5,
                "f1": 2 / 3,
            },
        ),
    ]
    save_config_results(exp_dir, "baseline", completed)

    runner = CliRunner()
    result = runner.invoke(main, ["experiment", "aggregate", str(exp_dir), "--no-warn"])
    assert result.exit_code == 0, result.output

    aggregate = json.loads(
        (exp_dir / "reports" / "aggregate.json").read_text(encoding="utf-8")
    )
    summary = aggregate["config_summaries"]["baseline"]
    # Headline reward
    assert summary["mean_reward"] == pytest.approx(0.75)
    assert summary["mean_automated_score"] == pytest.approx(0.75)
    # ir_diagnostics block
    assert summary["ir_diagnostics"]["mean_precision"] == pytest.approx(0.625)
    assert summary["ir_diagnostics"]["mean_recall"] == pytest.approx(0.75)
    assert summary["ir_diagnostics"]["mean_f1"] == pytest.approx(
        (0.40 + 2 / 3) / 2
    )
    # Back-compat flat fields still populated
    assert summary["mean_precision"] == pytest.approx(0.625)
    assert summary["mean_recall"] == pytest.approx(0.75)
    assert summary["mean_f1"] == pytest.approx((0.40 + 2 / 3) / 2)


__all__ = [
    "TestScoreFileListReward",
    "TestScoreSymbolListReward",
    "TestContinuousScorerReward",
    "test_aggregate_emits_mean_reward_and_ir_diagnostics",
]
