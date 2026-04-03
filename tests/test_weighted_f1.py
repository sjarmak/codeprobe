"""Tests for weighted F1 scoring with oracle_tiers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.mining.org_scale_oracle import _weighted_f1, oracle_check
from codeprobe.models.task import TaskVerification

# ---------------------------------------------------------------------------
# TaskVerification.oracle_tiers field
# ---------------------------------------------------------------------------


class TestTaskVerificationOracleTiers:
    def test_default_empty_dict(self) -> None:
        tv = TaskVerification()
        assert tv.oracle_tiers == {}

    def test_with_tiers(self) -> None:
        tiers = {"a.go": "required", "b.go": "context"}
        tv = TaskVerification(oracle_tiers=tiers)
        assert tv.oracle_tiers == tiers

    def test_frozen_field_ref(self) -> None:
        tv = TaskVerification(oracle_tiers={"a.go": "required"})
        with pytest.raises(AttributeError):
            tv.oracle_tiers = {}  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _weighted_f1 unit tests
# ---------------------------------------------------------------------------


class TestWeightedF1Function:
    def test_all_required_same_as_standard(self) -> None:
        """When all files are 'required', weighted_f1 == standard f1."""
        expected = frozenset({"a.go", "b.go", "c.go"})
        actual = frozenset({"a.go", "b.go"})
        tiers: dict[str, str] = {}  # missing → defaults to required

        result = _weighted_f1(expected, actual, tiers)

        # Standard: precision=2/2=1.0, recall=2/3, f1=2*1*(2/3)/(1+2/3)=0.8
        precision = 2 / 2
        recall = 2 / 3
        std_f1 = 2 * precision * recall / (precision + recall)

        assert result["weighted_f1"] == pytest.approx(round(std_f1, 4), abs=1e-4)
        assert result["weighted_recall"] == pytest.approx(round(recall, 4), abs=1e-4)

    def test_mixed_required_supplementary(self) -> None:
        """Required files weigh 2x, supplementary 1x."""
        expected = frozenset({"req.go", "sup.go"})
        actual = frozenset({"req.go"})  # only found required
        tiers = {"req.go": "required", "sup.go": "supplementary"}

        result = _weighted_f1(expected, actual, tiers)

        # weighted_total = 2.0 + 1.0 = 3.0
        # weighted_hit = 2.0 (only req.go matched)
        # weighted_recall = 2/3
        # precision = 1/1 = 1.0
        # weighted_f1 = 2*1*(2/3)/(1+2/3) = 0.8
        assert result["weighted_recall"] == pytest.approx(round(2 / 3, 4), abs=1e-4)
        expected_wf1 = 2.0 * 1.0 * (2 / 3) / (1.0 + 2 / 3)
        assert result["weighted_f1"] == pytest.approx(round(expected_wf1, 4), abs=1e-4)

    def test_mixed_with_context(self) -> None:
        """Context files weigh 0.5x."""
        expected = frozenset({"req.go", "ctx.go"})
        actual = frozenset({"ctx.go"})  # only found context file
        tiers = {"req.go": "required", "ctx.go": "context"}

        result = _weighted_f1(expected, actual, tiers)

        # weighted_total = 2.0 + 0.5 = 2.5
        # weighted_hit = 0.5 (only ctx.go matched)
        # weighted_recall = 0.5/2.5 = 0.2
        # precision = 1/1 = 1.0
        # weighted_f1 = 2*1*0.2/(1+0.2) = 0.3333
        assert result["weighted_recall"] == pytest.approx(round(0.5 / 2.5, 4), abs=1e-4)

    def test_empty_tiers_defaults_to_required(self) -> None:
        """Empty oracle_tiers → all files treated as required → same as standard."""
        expected = frozenset({"a.go", "b.go"})
        actual = frozenset({"a.go"})
        tiers: dict[str, str] = {}

        result = _weighted_f1(expected, actual, tiers)

        # All default to required (weight=2.0)
        # weighted_recall = 2.0/4.0 = 0.5 = standard recall 1/2
        assert result["weighted_recall"] == pytest.approx(0.5, abs=1e-4)

    def test_no_overlap(self) -> None:
        expected = frozenset({"a.go"})
        actual = frozenset({"x.go"})
        tiers: dict[str, str] = {}

        result = _weighted_f1(expected, actual, tiers)
        assert result["weighted_f1"] == 0.0
        assert result["weighted_recall"] == 0.0


# ---------------------------------------------------------------------------
# oracle_check with metric='weighted_f1'
# ---------------------------------------------------------------------------


def _setup_task(
    tmp_path: Path,
    expected: list[str],
    agent_answer: list[str],
    oracle_tiers: dict[str, str] | None = None,
) -> Path:
    """Create a task dir with ground_truth.json and answer.txt."""
    task_dir = tmp_path / "task_wf1"
    task_dir.mkdir(exist_ok=True)
    gt: dict = {
        "oracle_type": "file_list",
        "expected": expected,
        "commit": "abc123",
    }
    if oracle_tiers is not None:
        gt["oracle_tiers"] = oracle_tiers
    (task_dir / "ground_truth.json").write_text(json.dumps(gt))
    (task_dir / "answer.txt").write_text("\n".join(agent_answer) + "\n")
    return task_dir


class TestOracleCheckWeightedF1:
    def test_weighted_f1_returns_both_keys(self, tmp_path: Path) -> None:
        """Result dict has both 'f1' and 'weighted_f1' keys."""
        task_dir = _setup_task(
            tmp_path,
            expected=["a.go", "b.go"],
            agent_answer=["a.go", "b.go"],
            oracle_tiers={"a.go": "required", "b.go": "supplementary"},
        )
        result = oracle_check(task_dir, metric="weighted_f1")
        assert "f1" in result
        assert "weighted_f1" in result
        assert "weighted_recall" in result

    def test_score_is_weighted_f1(self, tmp_path: Path) -> None:
        """When metric='weighted_f1', score key holds the weighted_f1 value."""
        task_dir = _setup_task(
            tmp_path,
            expected=["a.go", "b.go"],
            agent_answer=["a.go", "b.go"],
            oracle_tiers={"a.go": "required", "b.go": "supplementary"},
        )
        result = oracle_check(task_dir, metric="weighted_f1")
        assert result["score"] == result["weighted_f1"]

    def test_exact_match_all_tiers(self, tmp_path: Path) -> None:
        """Exact match → weighted_f1 = 1.0 regardless of tiers."""
        task_dir = _setup_task(
            tmp_path,
            expected=["a.go", "b.go", "c.go"],
            agent_answer=["a.go", "b.go", "c.go"],
            oracle_tiers={
                "a.go": "required",
                "b.go": "supplementary",
                "c.go": "context",
            },
        )
        result = oracle_check(task_dir, metric="weighted_f1")
        assert result["weighted_f1"] == 1.0
        assert result["f1"] == 1.0

    def test_missing_oracle_tiers_key(self, tmp_path: Path) -> None:
        """No oracle_tiers in JSON → defaults to required → same as f1."""
        task_dir = _setup_task(
            tmp_path,
            expected=["a.go", "b.go"],
            agent_answer=["a.go"],
            oracle_tiers=None,  # key not written to JSON
        )
        result_wf1 = oracle_check(task_dir, metric="weighted_f1")
        result_f1 = oracle_check(task_dir, metric="f1")
        assert result_wf1["weighted_f1"] == result_f1["f1"]

    def test_empty_oracle_tiers_defaults(self, tmp_path: Path) -> None:
        """Empty oracle_tiers dict → all default to required → same as f1."""
        task_dir = _setup_task(
            tmp_path,
            expected=["a.go", "b.go"],
            agent_answer=["a.go"],
            oracle_tiers={},
        )
        result_wf1 = oracle_check(task_dir, metric="weighted_f1")
        result_f1 = oracle_check(task_dir, metric="f1")
        assert result_wf1["weighted_f1"] == result_f1["f1"]

    def test_f1_path_untouched(self, tmp_path: Path) -> None:
        """Standard f1 metric still works, no weighted keys in result."""
        task_dir = _setup_task(
            tmp_path,
            expected=["a.go", "b.go"],
            agent_answer=["a.go", "b.go"],
            oracle_tiers={"a.go": "required", "b.go": "supplementary"},
        )
        result = oracle_check(task_dir, metric="f1")
        assert "weighted_f1" not in result
        assert result["score"] == result["f1"]

    def test_weighted_score_differs_from_standard(self, tmp_path: Path) -> None:
        """When tiers vary, weighted_f1 differs from standard f1."""
        task_dir = _setup_task(
            tmp_path,
            expected=["req.go", "ctx.go"],
            agent_answer=["req.go"],
            oracle_tiers={"req.go": "required", "ctx.go": "context"},
        )
        result = oracle_check(task_dir, metric="weighted_f1")

        # Standard: precision=1, recall=0.5, f1=2/3
        # Weighted: precision=1, weighted_recall=2.0/2.5=0.8, wf1=2*1*0.8/1.8=0.8889
        assert result["f1"] == pytest.approx(round(2 / 3, 4), abs=1e-4)
        assert result["weighted_f1"] > result["f1"]

    def test_empty_answer_weighted(self, tmp_path: Path) -> None:
        """Empty agent answer → score 0 even for weighted_f1."""
        task_dir = _setup_task(
            tmp_path,
            expected=["a.go"],
            agent_answer=[],
            oracle_tiers={"a.go": "required"},
        )
        result = oracle_check(task_dir, metric="weighted_f1")
        assert result["score"] == 0.0


# ---------------------------------------------------------------------------
# Parametrized comprehensive tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expected,answer,tiers,expected_wf1",
    [
        pytest.param(
            ["a.go", "b.go"],
            ["a.go", "b.go"],
            {},
            1.0,
            id="all-required-exact-match",
        ),
        pytest.param(
            ["a.go", "b.go"],
            ["a.go"],
            {},
            # All required: precision=1, recall=0.5, f1=2/3
            round(2 * 1.0 * 0.5 / (1.0 + 0.5), 4),
            id="all-required-partial",
        ),
        pytest.param(
            ["req.go", "sup.go"],
            ["req.go"],
            {"req.go": "required", "sup.go": "supplementary"},
            # precision=1, w_recall=2/3, wf1=2*1*(2/3)/(1+2/3)=0.8
            round(2 * 1.0 * (2 / 3) / (1.0 + 2 / 3), 4),
            id="mixed-req-sup",
        ),
        pytest.param(
            ["req.go", "ctx.go"],
            ["req.go"],
            {"req.go": "required", "ctx.go": "context"},
            # precision=1, w_recall=2/2.5=0.8, wf1=2*1*0.8/1.8
            round(2 * 1.0 * 0.8 / 1.8, 4),
            id="mixed-req-ctx",
        ),
        pytest.param(
            ["a.go", "b.go", "c.go"],
            ["a.go", "b.go", "c.go"],
            {"a.go": "required", "b.go": "supplementary", "c.go": "context"},
            1.0,
            id="all-tiers-exact-match",
        ),
    ],
)
def test_weighted_f1_parametrized(
    tmp_path: Path,
    expected: list[str],
    answer: list[str],
    tiers: dict[str, str],
    expected_wf1: float,
) -> None:
    task_dir = _setup_task(tmp_path, expected, answer, tiers if tiers else None)
    result = oracle_check(task_dir, metric="weighted_f1")
    assert result["weighted_f1"] == pytest.approx(expected_wf1, abs=1e-3)
