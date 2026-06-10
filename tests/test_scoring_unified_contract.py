"""Unified ScoreResult contract — codeprobe-ufra (revised 2026-04-30).

Pins the post-voxa contract additions:

* ``ScoreResult.reward`` mirrors ``score`` (so callers reading either name
  agree).
* ``oracle_overlap_fbeta`` is registered, scores via F-beta(precision,
  recall, beta), reads beta from ``verification.fbeta_beta``, and falls
  back to F1-equivalent (beta=1.0) when the field is missing or invalid.
* The bead-spec contract — ``reward``, ``scorer_family``, ``sub_scores``,
  and ``diagnostics`` containing ``task_time_seconds`` / ``token_cost_usd``
  / ``ir_metrics`` — is emitted by the executor's scoring.json writer.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from codeprobe.core.executor import _save_task_artifacts
from codeprobe.core.scoring import (
    DEFAULT_FBETA_BETA,
    SCORER_FAMILIES,
    ScoreResult,
    _fbeta,
    _ir_reward_from_family,
    _read_fbeta_beta,
    score_file_list,
    score_symbol_list,
)
from codeprobe.models.experiment import CompletedTask
from codeprobe.core.executor import TaskResult


# ---------------------------------------------------------------------------
# ScoreResult.reward mirrors score
# ---------------------------------------------------------------------------


class TestScoreResultRewardMirror:
    def test_reward_defaults_to_score(self) -> None:
        r = ScoreResult(score=0.73, passed=True)
        assert r.reward == pytest.approx(0.73)
        assert r.score == pytest.approx(0.73)

    def test_reward_field_serializable(self) -> None:
        # Verify the field surfaces on dataclass introspection — used by
        # callers that asdict() the result.
        from dataclasses import asdict

        r = ScoreResult(score=0.42, passed=False)
        d = asdict(r)
        assert d["reward"] == pytest.approx(0.42)
        assert d["score"] == pytest.approx(0.42)

    def test_explicit_matching_reward_accepted(self) -> None:
        # Passing the alias explicitly is redundant but legal when it
        # agrees with score.
        r = ScoreResult(score=1.0, passed=True, reward=1.0, reward_score=1.0)
        assert r.reward == pytest.approx(1.0)
        assert r.reward_score == pytest.approx(1.0)

    def test_contradictory_reward_rejected(self) -> None:
        # ``reward`` / ``reward_score`` are aliases of ``score`` by
        # contract ("always equal by definition"); a contradictory value
        # means a bug at the construction site and must fail loud rather
        # than ship two different headline numbers.
        with pytest.raises(ValueError, match="contradicts score"):
            ScoreResult(score=1.0, passed=True, reward=0.85)
        with pytest.raises(ValueError, match="contradicts score"):
            ScoreResult(score=1.0, passed=True, reward_score=0.85)


# ---------------------------------------------------------------------------
# oracle_overlap_fbeta family
# ---------------------------------------------------------------------------


class TestFbetaFamilyRegistered:
    def test_family_is_registered(self) -> None:
        assert "oracle_overlap_fbeta" in SCORER_FAMILIES

    def test_default_beta_is_one(self) -> None:
        # F-beta with beta=1 is numerically equal to F1.
        precision, recall = 0.5, 0.8
        f1 = 2 * precision * recall / (precision + recall)
        assert _fbeta(precision, recall, 1.0) == pytest.approx(f1)

    def test_lower_beta_favours_precision(self) -> None:
        # beta < 1 → precision weighted more. With p=0.5, r=1.0:
        # F1 = 2/3 ≈ 0.667; F0.5 = (1.25*0.5*1.0) / (0.25*0.5 + 1.0) = 0.555…
        precision, recall = 0.5, 1.0
        f1 = _fbeta(precision, recall, 1.0)
        f_half = _fbeta(precision, recall, 0.5)
        assert f_half < f1

    def test_higher_beta_favours_recall(self) -> None:
        # Symmetric: beta > 1 should reward recall more.
        precision, recall = 1.0, 0.5
        f1 = _fbeta(precision, recall, 1.0)
        f_two = _fbeta(precision, recall, 2.0)
        assert f_two < f1

    def test_zero_intersection_returns_zero(self) -> None:
        assert _fbeta(0.0, 0.0, 0.5) == pytest.approx(0.0)

    def test_invalid_beta_falls_back_to_default(self) -> None:
        # Negative or zero beta is invalid → coerced to default (1.0).
        precision, recall = 0.5, 0.5
        f1 = _fbeta(precision, recall, 1.0)
        assert _fbeta(precision, recall, 0.0) == pytest.approx(f1)
        assert _fbeta(precision, recall, -1.0) == pytest.approx(f1)


class TestFbetaScoreFileList:
    def test_default_beta_collapses_to_f1(self) -> None:
        # With no metadata, beta=1.0; reward should match F1.
        expected = ["a.py", "b.py"]
        actual = expected + [f"noise_{i}.py" for i in range(8)]
        result = score_file_list(
            expected, actual, family="oracle_overlap_fbeta", beta=1.0
        )
        assert result.scorer_family == "oracle_overlap_fbeta"
        f1 = result.sub_scores["f1"]
        assert result.score == pytest.approx(f1)
        assert result.reward == pytest.approx(f1)
        assert result.sub_scores["fbeta_beta"] == pytest.approx(1.0)

    def test_low_beta_punishes_overship(self) -> None:
        # 2 expected, 8 noise = recall 1.0, precision 0.2.
        # F1 ≈ 0.333; F0.5 should be lower because precision weighs more.
        expected = ["a.py", "b.py"]
        actual = expected + [f"noise_{i}.py" for i in range(8)]
        f1_result = score_file_list(
            expected, actual, family="oracle_overlap_fbeta", beta=1.0
        )
        fhalf_result = score_file_list(
            expected, actual, family="oracle_overlap_fbeta", beta=0.5
        )
        assert fhalf_result.score < f1_result.score


class TestFbetaScoreSymbolList:
    def test_default_beta_matches_f1(self) -> None:
        result = score_symbol_list(
            ["foo", "bar"],
            ["foo", "bar"],
            family="oracle_overlap_fbeta",
            beta=1.0,
        )
        assert result.score == pytest.approx(1.0)
        assert result.scorer_family == "oracle_overlap_fbeta"
        assert result.sub_scores["fbeta_beta"] == pytest.approx(1.0)


class TestReadFbetaBeta:
    def test_returns_default_for_no_task_dir(self) -> None:
        assert _read_fbeta_beta(None) == pytest.approx(DEFAULT_FBETA_BETA)

    def test_returns_default_when_no_metadata(self, tmp_path: Path) -> None:
        assert _read_fbeta_beta(tmp_path) == pytest.approx(DEFAULT_FBETA_BETA)

    def test_reads_explicit_beta(self, tmp_path: Path) -> None:
        meta = {"verification": {"fbeta_beta": 0.5}}
        (tmp_path / "metadata.json").write_text(json.dumps(meta))
        assert _read_fbeta_beta(tmp_path) == pytest.approx(0.5)

    def test_falls_back_on_negative(self, tmp_path: Path) -> None:
        meta = {"verification": {"fbeta_beta": -1.0}}
        (tmp_path / "metadata.json").write_text(json.dumps(meta))
        assert _read_fbeta_beta(tmp_path) == pytest.approx(DEFAULT_FBETA_BETA)

    def test_falls_back_on_non_numeric(self, tmp_path: Path) -> None:
        meta = {"verification": {"fbeta_beta": "loose"}}
        (tmp_path / "metadata.json").write_text(json.dumps(meta))
        assert _read_fbeta_beta(tmp_path) == pytest.approx(DEFAULT_FBETA_BETA)


# ---------------------------------------------------------------------------
# Executor scoring.json — bead-required diagnostics keys
# ---------------------------------------------------------------------------


class TestExecutorDiagnosticsContract:
    def test_scoring_json_has_reward_and_diagnostics(self, tmp_path: Path) -> None:
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()

        completed = CompletedTask(
            task_id="t1",
            automated_score=0.42,
            duration_seconds=12.5,
            cost_usd=0.0034,
            scoring_details={
                "passed": False,
                "scorer_family": "oracle_overlap_fbeta",
                "sub_scores": {"f1": 0.42, "reward": 0.42, "fbeta_beta": 0.5},
                "diagnostics": {
                    "ir_metrics": {"precision": 0.3, "recall": 0.7, "f1": 0.42}
                },
            },
        )
        result = TaskResult(
            completed=completed, agent_stdout="", agent_stderr=""
        )

        _save_task_artifacts(runs_dir, "t1", result)

        scoring_path = runs_dir / "t1" / "scoring.json"
        assert scoring_path.is_file()
        scoring = json.loads(scoring_path.read_text())

        # Bead contract: reward + scorer_family + sub_scores + diagnostics
        assert scoring["reward"] == pytest.approx(0.42)
        assert scoring["score"] == pytest.approx(0.42)
        assert scoring["scorer_family"] == "oracle_overlap_fbeta"
        assert "sub_scores" in scoring

        diag = scoring["diagnostics"]
        assert diag["task_time_seconds"] == pytest.approx(12.5)
        assert diag["token_cost_usd"] == pytest.approx(0.0034)
        assert diag["ir_metrics"]["recall"] == pytest.approx(0.7)

    def test_scoring_json_omits_token_cost_when_unavailable(
        self, tmp_path: Path
    ) -> None:
        # cost_usd=None (unavailable) → omit token_cost_usd, do not write a
        # bogus zero. task_time_seconds always present (duration_seconds
        # defaults to 0.0).
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        completed = CompletedTask(
            task_id="t1",
            automated_score=1.0,
            scoring_details={"passed": True, "scorer_family": "binary_test"},
        )
        result = TaskResult(
            completed=completed, agent_stdout="", agent_stderr=""
        )
        _save_task_artifacts(runs_dir, "t1", result)

        scoring = json.loads((runs_dir / "t1" / "scoring.json").read_text())
        diag = scoring["diagnostics"]
        assert "task_time_seconds" in diag
        assert "token_cost_usd" not in diag
        # Same null-handling for raw token counts: omit when unavailable
        # so callers can distinguish "agent reported zero" from "adapter
        # didn't capture telemetry".
        assert "input_tokens" not in diag
        assert "output_tokens" not in diag

    def test_scoring_json_emits_raw_token_counts(self, tmp_path: Path) -> None:
        # codeprobe-oktg: scoring.json.diagnostics carries input_tokens /
        # output_tokens alongside token_cost_usd. Cost-Pareto plots that
        # aren't dollar-locked need the raw counts.
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        completed = CompletedTask(
            task_id="t1",
            automated_score=0.42,
            duration_seconds=12.5,
            cost_usd=0.0034,
            input_tokens=1234,
            output_tokens=567,
            scoring_details={
                "passed": False,
                "scorer_family": "oracle_overlap_f1",
            },
        )
        result = TaskResult(completed=completed, agent_stdout="", agent_stderr="")

        _save_task_artifacts(runs_dir, "t1", result)

        scoring = json.loads((runs_dir / "t1" / "scoring.json").read_text())
        diag = scoring["diagnostics"]
        # No regression on the existing cost field.
        assert diag["token_cost_usd"] == pytest.approx(0.0034)
        assert diag["input_tokens"] == 1234
        assert diag["output_tokens"] == 567

    def test_scoring_json_emits_cache_token_counts(self, tmp_path: Path) -> None:
        # codeprobe-e9pr: scoring.json.diagnostics carries cache_read_tokens
        # and cache_creation_tokens alongside the existing input/output
        # counts. Cache-aware cost-Pareto plots need bulk-reuse and
        # write-through tokens broken out from the uncached input portion.
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        completed = CompletedTask(
            task_id="t1",
            automated_score=0.42,
            duration_seconds=12.5,
            cost_usd=0.0034,
            input_tokens=30,
            output_tokens=567,
            cache_read_tokens=98765,
            cache_creation_tokens=4321,
            scoring_details={
                "passed": False,
                "scorer_family": "oracle_overlap_f1",
            },
        )
        result = TaskResult(completed=completed, agent_stdout="", agent_stderr="")

        _save_task_artifacts(runs_dir, "t1", result)

        scoring = json.loads((runs_dir / "t1" / "scoring.json").read_text())
        diag = scoring["diagnostics"]
        # Existing fields remain (no regression).
        assert diag["token_cost_usd"] == pytest.approx(0.0034)
        assert diag["input_tokens"] == 30
        assert diag["output_tokens"] == 567
        # New fields populated.
        assert diag["cache_read_tokens"] == 98765
        assert diag["cache_creation_tokens"] == 4321

    def test_scoring_json_omits_cache_tokens_when_unavailable(
        self, tmp_path: Path
    ) -> None:
        # When the adapter doesn't capture cache telemetry, cache fields
        # are omitted (not zero-filled) so callers can distinguish "no
        # cache hits" from "adapter didn't record cache data".
        runs_dir = tmp_path / "runs"
        runs_dir.mkdir()
        completed = CompletedTask(
            task_id="t1",
            automated_score=1.0,
            input_tokens=10,
            output_tokens=20,
            scoring_details={"passed": True, "scorer_family": "binary_test"},
        )
        result = TaskResult(completed=completed, agent_stdout="", agent_stderr="")

        _save_task_artifacts(runs_dir, "t1", result)

        scoring = json.loads((runs_dir / "t1" / "scoring.json").read_text())
        diag = scoring["diagnostics"]
        assert "cache_read_tokens" not in diag
        assert "cache_creation_tokens" not in diag


# ---------------------------------------------------------------------------
# _ir_reward_from_family routes oracle_overlap_fbeta correctly
# ---------------------------------------------------------------------------


class TestIrRewardRoutingFbeta:
    def test_fbeta_family_uses_fbeta(self) -> None:
        precision, recall, f1 = 0.5, 0.8, 0.6153
        reward_default = _ir_reward_from_family(
            "oracle_overlap_fbeta",
            precision=precision,
            recall=recall,
            f1=f1,
            beta=1.0,
        )
        # beta=1.0 → matches F1 (subject to rounding of the input f1).
        assert reward_default == pytest.approx(_fbeta(precision, recall, 1.0))

        reward_low = _ir_reward_from_family(
            "oracle_overlap_fbeta",
            precision=precision,
            recall=recall,
            f1=f1,
            beta=0.5,
        )
        assert reward_low == pytest.approx(_fbeta(precision, recall, 0.5))
        assert reward_low < reward_default  # beta<1 punishes recall-heavy

    def test_unknown_family_falls_back_to_f1(self) -> None:
        # The lib already had this contract; pin it again so the
        # fbeta routing addition didn't break it.
        result = _ir_reward_from_family(
            "imaginary_family",
            precision=0.5,
            recall=0.8,
            f1=0.6,
            beta=2.0,  # ignored
        )
        assert result == pytest.approx(0.6)
