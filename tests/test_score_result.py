"""Tests for ScoreResult dataclass — default details and round-trip.

Also exercises ArtifactScorer.score() for the file_list answer_type to
verify the new PASS_THRESHOLD semantics (f1 >= 0.5).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.analysis.stats import PASS_THRESHOLD
from codeprobe.core.scoring import ArtifactScorer, ScoreResult

# ---------------------------------------------------------------------------
# ScoreResult.details field
# ---------------------------------------------------------------------------


class TestScoreResultDetails:
    def test_default_details_is_empty_dict(self) -> None:
        """ScoreResult() with no args must default details to {}."""
        result = ScoreResult(score=0.0, passed=False)
        assert result.details == {}

    def test_default_details_is_independent_per_instance(self) -> None:
        """Default dicts must not be shared between instances (field factory)."""
        a = ScoreResult(score=0.0, passed=False)
        b = ScoreResult(score=1.0, passed=True)
        # Mutating one must not leak into the other — confirms field(default_factory=dict)
        a.details["x"] = 1
        assert b.details == {}

    def test_details_round_trip(self) -> None:
        """ScoreResult(score=0.5, details={'a': 1}) round-trips correctly."""
        result = ScoreResult(score=0.5, passed=True, details={"a": 1})
        assert result.score == pytest.approx(0.5)
        assert result.passed is True
        assert result.details == {"a": 1}

    def test_existing_positional_constructions_still_work(self) -> None:
        """Pre-existing code constructs ScoreResult(score=..., passed=..., error=...).
        Those constructions must still produce empty details by default.
        """
        result = ScoreResult(score=0.0, passed=False, error="boom")
        assert result.error == "boom"
        assert result.details == {}


# ---------------------------------------------------------------------------
# PASS_THRESHOLD centralization
# ---------------------------------------------------------------------------


class TestPassThresholdConstant:
    def test_pass_threshold_value(self) -> None:
        """PASS_THRESHOLD is 0.5 as the centralized pass/fail cutoff."""
        assert PASS_THRESHOLD == 0.5


# ---------------------------------------------------------------------------
# ArtifactScorer file_list uses PASS_THRESHOLD (f1 >= 0.5)
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _make_file_list_task(
    tmp_path: Path, expected: list[str], actual: list[str]
) -> Path:
    _write_json(
        tmp_path / "ground_truth.json",
        {"answer_type": "file_list", "answer": expected, "confidence": 0.9},
    )
    _write_json(tmp_path / "answer.json", {"answer": actual})
    return tmp_path


class TestArtifactScorerFileListThreshold:
    def test_f1_zero_is_fail(self, tmp_path: Path) -> None:
        """f1 = 0.0 must be passed=False."""
        task_dir = _make_file_list_task(tmp_path, expected=["a.py"], actual=["z.py"])
        result = ArtifactScorer().score("", task_dir)
        assert result.score == pytest.approx(0.0)
        assert result.passed is False

    def test_f1_below_threshold_is_fail(self, tmp_path: Path) -> None:
        """f1 = 0.4 (below PASS_THRESHOLD) must be passed=False.

        Use expected=[a,b,c,d,e,f], actual=[a,b] →
        precision = 2/2 = 1.0
        recall    = 2/6 ≈ 0.333
        f1        = 2 * (1.0 * 0.333) / (1.333) ≈ 0.5

        Tweak to get f1 ≈ 0.4:
        expected=[a,b,c,d,e,f,g], actual=[a,b] →
        precision=1.0, recall=2/7≈0.286, f1=2*0.286/1.286≈0.444

        Better: expected=[a,b,c,d,e,f,g,h,i], actual=[a,b] →
        precision=1.0, recall=2/9≈0.222, f1=2*0.222/1.222≈0.364 (<0.5)
        """
        task_dir = _make_file_list_task(
            tmp_path,
            expected=[f"f{i}.py" for i in range(9)],
            actual=["f0.py", "f1.py"],
        )
        result = ArtifactScorer().score("", task_dir)
        # f1 ≈ 0.364 — below PASS_THRESHOLD=0.5
        assert result.score < PASS_THRESHOLD
        assert result.score > 0.0
        assert result.passed is False

    def test_f1_equal_threshold_is_pass(self, tmp_path: Path) -> None:
        """f1 exactly 0.5 must be passed=True (>= semantic)."""
        # expected=[a,b,c], actual=[a] → p=1.0, r=1/3, f1=2*(1/3)/(4/3)=0.5
        task_dir = _make_file_list_task(
            tmp_path,
            expected=["a.py", "b.py", "c.py"],
            actual=["a.py"],
        )
        result = ArtifactScorer().score("", task_dir)
        assert result.score == pytest.approx(0.5, abs=0.01)
        assert result.passed is True

    def test_f1_above_threshold_is_pass(self, tmp_path: Path) -> None:
        """f1 = 0.6+ (above PASS_THRESHOLD) must be passed=True.

        expected=[a,b], actual=[a,b,c,d] → p=0.5, r=1.0, f1=2*0.5/1.5≈0.667
        """
        task_dir = _make_file_list_task(
            tmp_path,
            expected=["a.py", "b.py"],
            actual=["a.py", "b.py", "c.py", "d.py"],
        )
        result = ArtifactScorer().score("", task_dir)
        assert result.score >= PASS_THRESHOLD
        assert result.passed is True
