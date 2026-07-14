"""Reward + scorer_family contract — codeprobe-voxa (revised 2026-04-30).

Pins the per-task scorer_family routing landed in the reopened voxa pass:

* Default IR reward is **F1** (`oracle_overlap_f1`) — agents that dump
  the entire repo no longer score 1.0. Recall stays in
  ``sub_scores`` / ``ir_metrics`` for diagnostics.
* File-discovery / triage tasks opt into ``oracle_overlap_recall`` and
  recover the recall-tilted reward where over-shipping is free.
* The on-disk continuous oracle routes via
  ``verification.scorer_family`` from ``metadata.json``.
* ``ScoreResult`` carries ``scorer_family`` + ``sub_scores`` +
  ``diagnostics.ir_metrics`` for every IR-style result.

Per A6 each registered family has null / golden / adversarial-dump
fixtures so a future contract change has to face the canonical cases.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from codeprobe.core.scoring import (
    SCORER_FAMILIES,
    ArtifactScorer,
    BinaryScorer,
    ContinuousScorer,
    score_count,
    score_dependency_chain,
    score_exact_match,
    score_file_list,
    score_symbol_list,
)

# ---------------------------------------------------------------------------
# Default IR family is F1 — adversarial dump must NOT score 1.0
# ---------------------------------------------------------------------------


class TestScoreFileListDefaultFamilyIsF1:
    def test_default_family_label(self) -> None:
        result = score_file_list(["a.py", "b.py"], ["a.py", "b.py"])
        assert result.scorer_family == "oracle_overlap_f1"
        assert result.scorer_family in SCORER_FAMILIES

    def test_exact_match_reward_is_one(self) -> None:
        result = score_file_list(["a.py", "b.py"], ["a.py", "b.py"])
        assert result.score == pytest.approx(1.0)
        assert result.passed is True
        assert result.sub_scores["recall"] == pytest.approx(1.0)
        assert result.sub_scores["precision"] == pytest.approx(1.0)
        assert result.sub_scores["f1"] == pytest.approx(1.0)
        assert result.sub_scores["reward"] == pytest.approx(1.0)
        assert result.diagnostics["ir_metrics"]["f1"] == pytest.approx(1.0)

    def test_adversarial_dump_drops_below_pass_threshold(self) -> None:
        """A6 / A3 acceptance: agent dumps 98 noise files alongside the
        2 correct ones → recall=1.0, F1≈0.039. Reward MUST be the F1
        under the default family so the dump doesn't fake a 1.0.
        """
        expected = ["a.py", "b.py"]
        actual = expected + [f"noise_{i}.py" for i in range(98)]
        result = score_file_list(expected, actual)
        assert result.scorer_family == "oracle_overlap_f1"
        assert result.score == pytest.approx(0.0392, abs=1e-3)
        assert result.score < 0.5  # A3
        assert result.passed is False
        # Recall is still surfaced in diagnostics so reviewers can see
        # the agent did "find everything" — they just shipped noise.
        assert result.sub_scores["recall"] == pytest.approx(1.0)
        assert result.sub_scores["precision"] == pytest.approx(0.02)
        assert result.ir_metrics["recall"] == pytest.approx(1.0)
        assert result.diagnostics["ir_metrics"]["recall"] == pytest.approx(1.0)

    def test_modest_overship_lands_at_f1(self) -> None:
        """Two extra files on top of the two expected → F1 = 0.667."""
        result = score_file_list(["a.py", "b.py"], ["a.py", "b.py", "c.py", "d.py"])
        assert result.score == pytest.approx(2 / 3)
        assert result.sub_scores["recall"] == pytest.approx(1.0)
        assert result.sub_scores["precision"] == pytest.approx(0.5)
        assert result.passed is True

    def test_undership_lands_at_f1(self) -> None:
        """Agent finds 2 of 4 (recall=0.5, precision=1.0) → F1=0.667."""
        result = score_file_list(
            ["a.py", "b.py", "c.py", "d.py"],
            ["a.py", "b.py"],
        )
        assert result.score == pytest.approx(2 / 3)
        assert result.sub_scores["recall"] == pytest.approx(0.5)
        assert result.sub_scores["precision"] == pytest.approx(1.0)

    def test_null_input_zero_reward(self) -> None:
        result = score_file_list(["a.py"], ["z.py"])
        assert result.score == pytest.approx(0.0)
        assert result.passed is False
        assert result.scorer_family == "oracle_overlap_f1"

    def test_empty_actual_keeps_family_label(self) -> None:
        result = score_file_list(["a.py"], [])
        assert result.scorer_family == "oracle_overlap_f1"
        assert result.score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Opt-in family override — oracle_overlap_recall recovers recall reward
# ---------------------------------------------------------------------------


class TestScoreFileListRecallFamily:
    def test_recall_family_label(self) -> None:
        result = score_file_list(
            ["a.py"], ["a.py"], family="oracle_overlap_recall"
        )
        assert result.scorer_family == "oracle_overlap_recall"

    def test_recall_family_keeps_full_reward_on_extreme_overship(self) -> None:
        """Discovery / triage task: dump-and-filter is fine. Reward = recall."""
        expected = ["a.py", "b.py"]
        actual = expected + [f"noise_{i}.py" for i in range(98)]
        result = score_file_list(expected, actual, family="oracle_overlap_recall")
        assert result.score == pytest.approx(1.0)
        assert result.passed is True
        assert result.sub_scores["recall"] == pytest.approx(1.0)
        assert result.sub_scores["precision"] == pytest.approx(0.02)

    def test_recall_family_undership_drops_reward(self) -> None:
        result = score_file_list(
            ["a.py", "b.py"], ["a.py"], family="oracle_overlap_recall"
        )
        assert result.score == pytest.approx(0.5)
        assert result.sub_scores["recall"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# score_symbol_list — same routing
# ---------------------------------------------------------------------------


class TestScoreSymbolListFamily:
    def test_default_is_f1_and_adversarial_drops(self) -> None:
        expected = ["Foo", "Bar"]
        actual = expected + [f"Noise{i}" for i in range(50)]
        result = score_symbol_list(expected, actual)
        assert result.scorer_family == "oracle_overlap_f1"
        assert result.score < 0.5
        assert result.sub_scores["recall"] == pytest.approx(1.0)

    def test_recall_family_recovers_full_reward_on_overship(self) -> None:
        result = score_symbol_list(
            ["Foo"], ["Foo", "Bar", "Baz"], family="oracle_overlap_recall"
        )
        assert result.scorer_family == "oracle_overlap_recall"
        assert result.score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# 38223444-style honest-score case — A4
# ---------------------------------------------------------------------------


class TestUr8dHonestScoreSurvives:
    def test_38223444_with_sg_keeps_honest_score_under_default_family(self) -> None:
        """A4: precision=1.0, recall≈0.333 → F1≈0.5.

        38223444 with-sg shipped 2 correct files out of 6 expected
        (precision=1.0, recall=0.333). Under the new default, F1=0.5 —
        an honest "found 1/3 of what was asked, didn't oversell" score.
        Crucially it's NOT 1.0 (which the old recall-only family would
        have said since precision is moot under recall) and NOT 0.04
        (which the original F1-only family pre-voxa would have said for
        an over-shipping agent).
        """
        expected = ["x1.py", "x2.py", "x3.py", "x4.py", "x5.py", "x6.py"]
        actual = ["x1.py", "x2.py"]
        result = score_file_list(expected, actual)
        assert result.scorer_family == "oracle_overlap_f1"
        assert result.score == pytest.approx(0.5)
        assert result.sub_scores["recall"] == pytest.approx(1 / 3)
        assert result.sub_scores["precision"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# ContinuousScorer — family routing via metadata.json
# ---------------------------------------------------------------------------


def _make_oracle_task(
    tmp_path: Path,
    name: str,
    script: str,
    *,
    metadata: dict | None = None,
) -> Path:
    task_dir = tmp_path / name
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    test_sh = tests_dir / "test.sh"
    test_sh.write_text(script, encoding="utf-8")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    if metadata is not None:
        (task_dir / "metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
    return task_dir


_E5D7A4E7_SCRIPT = (
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


class TestContinuousScorerFamilyRouting:
    def test_default_family_is_f1_overship_drops_below_pass(
        self, tmp_path: Path
    ) -> None:
        """A3: e5d7a4e7-style overship → F1=0.41 < 0.5 under default family."""
        task_dir = _make_oracle_task(tmp_path, "default", _E5D7A4E7_SCRIPT)
        result = ContinuousScorer().score("output", task_dir)
        assert result.scorer_family == "oracle_overlap_f1"
        assert result.score == pytest.approx(0.4092)
        assert result.score < 0.5
        assert result.passed is False
        assert result.sub_scores["f1"] == pytest.approx(0.4092)
        assert result.sub_scores["recall"] == pytest.approx(1.0)
        assert result.diagnostics["ir_metrics"]["recall"] == pytest.approx(1.0)

    def test_explicit_recall_family_keeps_full_reward(self, tmp_path: Path) -> None:
        """Opt-in via metadata.json — recall is the reward, not F1."""
        task_dir = _make_oracle_task(
            tmp_path,
            "recall",
            _E5D7A4E7_SCRIPT,
            metadata={
                "verification": {"scorer_family": "oracle_overlap_recall"}
            },
        )
        result = ContinuousScorer().score("output", task_dir)
        assert result.scorer_family == "oracle_overlap_recall"
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_weighted_oracle_routes_to_weighted_f1(self, tmp_path: Path) -> None:
        """Org-scale weighted oracle — reward = weighted F1 (stored in `f1`).

        The on-disk oracle stores the weighted primary score in the ``f1``
        field when ``metric == "weighted_f1"``. Family routing picks
        ``oracle_weighted_f1`` automatically when ``weighted_recall`` is
        present and finite.
        """
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
        assert result.scorer_family == "oracle_weighted_f1"
        assert result.score == pytest.approx(0.55)
        assert result.sub_scores["weighted_recall"] == pytest.approx(0.85)
        assert result.sub_scores["f1"] == pytest.approx(0.55)

    def test_explicit_weighted_recall_family_recovers_recall_tilt(
        self, tmp_path: Path
    ) -> None:
        """Opt-in tier-weighted recall family for triage-style org-scale
        tasks where finding the high-tier files matters more than precision.
        """
        script = (
            "#!/bin/bash\n"
            'echo "0.55" > "$PWD/reward.txt"\n'
            'cat > "$PWD/metrics.json" <<\'JSON\'\n'
            '{"score": 0.55, "metric": "weighted_f1", "f1": 0.55, '
            '"precision": 0.6, "recall": 0.7, '
            '"weighted_recall": 0.85}\n'
            "JSON\n"
            "exit 0\n"
        )
        task_dir = _make_oracle_task(
            tmp_path,
            "weighted-recall",
            script,
            metadata={
                "verification": {"scorer_family": "oracle_weighted_recall"}
            },
        )
        result = ContinuousScorer().score("output", task_dir)
        assert result.scorer_family == "oracle_weighted_recall"
        assert result.score == pytest.approx(0.85)

    def test_legacy_oracle_without_metrics_json_falls_back_to_reward_txt(
        self, tmp_path: Path
    ) -> None:
        """No ``metrics.json`` → no IR data → score stays whatever
        ``reward.txt`` says, and the scorer_family becomes ``continuous``
        (no IR rubric was applied).
        """
        script = (
            "#!/bin/bash\n"
            'echo "0.5" > "$PWD/reward.txt"\n'
            "exit 0\n"
        )
        task_dir = _make_oracle_task(tmp_path, "legacy", script)
        result = ContinuousScorer().score("output", task_dir)
        assert result.scorer_family == "continuous"
        assert result.score == pytest.approx(0.5)
        assert result.ir_metrics == {}
        assert result.sub_scores == {"raw_score": pytest.approx(0.5)}

    def test_null_oracle_metrics_zeros_pass(self, tmp_path: Path) -> None:
        """Adversarial / null oracle — empty-set match → reward = 0."""
        script = (
            "#!/bin/bash\n"
            'echo "0" > "$PWD/reward.txt"\n'
            'cat > "$PWD/metrics.json" <<\'JSON\'\n'
            '{"f1": 0.0, "precision": 0.0, "recall": 0.0}\n'
            "JSON\n"
            "exit 0\n"
        )
        task_dir = _make_oracle_task(tmp_path, "null", script)
        result = ContinuousScorer().score("output", task_dir)
        assert result.scorer_family == "oracle_overlap_f1"
        assert result.score == pytest.approx(0.0)
        assert result.passed is False


# ---------------------------------------------------------------------------
# ArtifactScorer — family routing via metadata.json verification block
# ---------------------------------------------------------------------------


def _make_artifact_task(
    tmp_path: Path,
    name: str,
    *,
    expected: list[str],
    actual: list[str],
    metadata: dict | None = None,
) -> Path:
    task_dir = tmp_path / name
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "ground_truth.json").write_text(
        json.dumps(
            {
                "answer_type": "file_list",
                "answer": expected,
            }
        ),
        encoding="utf-8",
    )
    (task_dir / "answer.json").write_text(
        json.dumps({"answer_type": "file_list", "answer": actual}),
        encoding="utf-8",
    )
    if metadata is not None:
        (task_dir / "metadata.json").write_text(
            json.dumps(metadata), encoding="utf-8"
        )
    return task_dir


class TestArtifactScorerFamilyRouting:
    def test_default_family_is_f1_for_file_list_oracle(
        self, tmp_path: Path
    ) -> None:
        task_dir = _make_artifact_task(
            tmp_path,
            "default",
            expected=["a.py", "b.py"],
            actual=["a.py", "b.py"] + [f"n{i}.py" for i in range(48)],
        )
        result = ArtifactScorer().score("", task_dir)
        assert result.scorer_family == "oracle_overlap_f1"
        assert result.score < 0.5

    def test_metadata_override_routes_to_recall_family(
        self, tmp_path: Path
    ) -> None:
        task_dir = _make_artifact_task(
            tmp_path,
            "recall-opt-in",
            expected=["a.py", "b.py"],
            actual=["a.py", "b.py"] + [f"n{i}.py" for i in range(48)],
            metadata={"verification": {"scorer_family": "oracle_overlap_recall"}},
        )
        result = ArtifactScorer().score("", task_dir)
        assert result.scorer_family == "oracle_overlap_recall"
        assert result.score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Non-IR families — null + golden coverage
# ---------------------------------------------------------------------------


class TestExactMatchFamily:
    def test_count_match(self) -> None:
        result = score_count(5, 5)
        assert result.scorer_family == "exact_match"
        assert result.score == pytest.approx(1.0)
        assert result.sub_scores["match"] == pytest.approx(1.0)

    def test_count_mismatch(self) -> None:
        result = score_count(5, 7)
        assert result.scorer_family == "exact_match"
        assert result.score == pytest.approx(0.0)

    def test_text_match(self) -> None:
        result = score_exact_match("Yes", "yes")
        assert result.scorer_family == "exact_match"
        assert result.score == pytest.approx(1.0)


class TestSequenceLcsFamily:
    def test_exact_chain_match(self) -> None:
        result = score_dependency_chain(["a", "b", "c"], ["a", "b", "c"])
        assert result.scorer_family == "sequence_lcs"
        assert result.score == pytest.approx(1.0)
        assert result.sub_scores["lcs_length"] == 3

    def test_partial_chain_match(self) -> None:
        result = score_dependency_chain(["a", "b", "c"], ["a", "c"])
        assert result.scorer_family == "sequence_lcs"
        assert result.score == pytest.approx(2 / 3)

    def test_empty_chain(self) -> None:
        result = score_dependency_chain([], [])
        assert result.scorer_family == "sequence_lcs"
        assert result.score == pytest.approx(0.0)


class TestBinaryTestFamily:
    def test_exit_zero_is_one(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "binary"
        (task_dir / "tests").mkdir(parents=True)
        test_sh = task_dir / "tests" / "test.sh"
        test_sh.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        test_sh.chmod(0o755)
        result = BinaryScorer().score("", task_dir)
        assert result.scorer_family == "binary_test"
        assert result.score == pytest.approx(1.0)
        assert result.sub_scores["exit_code"] == 0

    def test_exit_nonzero_is_zero(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "binary-fail"
        (task_dir / "tests").mkdir(parents=True)
        test_sh = task_dir / "tests" / "test.sh"
        test_sh.write_text("#!/bin/bash\nexit 1\n", encoding="utf-8")
        test_sh.chmod(0o755)
        result = BinaryScorer().score("", task_dir)
        assert result.scorer_family == "binary_test"
        assert result.score == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# OracleChecksScorer — structured-rubric criteria with normalized weights
# ---------------------------------------------------------------------------


def _make_rubric_task(
    tmp_path: Path,
    name: str,
    criteria: list[dict],
    verifier_scripts: dict[str, str],
    *,
    on_disk: bool = True,
) -> Path:
    """Build a task dir with a rubric.json + verifier scripts.

    When ``on_disk`` is False, the rubric is omitted so callers can
    inject ``metadata_criteria`` via the constructor instead.
    """
    task_dir = tmp_path / name
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    if on_disk:
        (tests_dir / "rubric.json").write_text(
            json.dumps(criteria), encoding="utf-8"
        )

    verifiers_dir = tests_dir / "verifiers"
    verifiers_dir.mkdir(exist_ok=True)
    for script_name, script_content in verifier_scripts.items():
        script_path = verifiers_dir / script_name
        script_path.write_text(script_content)
        script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)

    return task_dir


class TestOracleChecksFamily:
    """Per the bead acceptance criteria (A1-A4):

    * full pass / partial / all-fail produce the documented reward
    * weights are *normalized*, not required to sum to 1.0
    * missing-criterion / malformed-rubric paths fail loudly
    """

    def test_oracle_checks_in_registry(self) -> None:
        from codeprobe.core.scoring import OracleChecksScorer

        assert "oracle_checks" in SCORER_FAMILIES
        assert OracleChecksScorer.SCORER_FAMILY == "oracle_checks"

    def test_full_pass(self, tmp_path: Path) -> None:
        from codeprobe.core.scoring import OracleChecksScorer

        criteria = [
            {"name": "edge_case", "weight": 0.5, "verifier": "edge.sh"},
            {"name": "error_branches", "weight": 0.5, "verifier": "errors.sh"},
        ]
        verifiers = {
            "edge.sh": '#!/bin/bash\necho \'{"score": 1.0, "passed": true}\'\nexit 0\n',
            "errors.sh": '#!/bin/bash\necho \'{"score": 1.0, "passed": true}\'\nexit 0\n',
        }
        task_dir = _make_rubric_task(tmp_path, "rubric-full-pass", criteria, verifiers)
        result = OracleChecksScorer().score("output", task_dir)

        assert result.scorer_family == "oracle_checks"
        assert result.score == pytest.approx(1.0)
        assert result.passed is True
        assert result.sub_scores["composite"] == pytest.approx(1.0)
        assert result.sub_scores["criterion_scores"]["edge_case"] == pytest.approx(1.0)
        assert result.sub_scores["criterion_scores"]["error_branches"] == pytest.approx(1.0)
        assert result.sub_scores["total_weight"] == pytest.approx(1.0)

    def test_partial_pass(self, tmp_path: Path) -> None:
        from codeprobe.core.scoring import OracleChecksScorer

        criteria = [
            {"name": "covered", "weight": 0.6, "verifier": "covered.sh"},
            {"name": "missed", "weight": 0.4, "verifier": "missed.sh"},
        ]
        verifiers = {
            "covered.sh": '#!/bin/bash\necho \'{"score": 1.0, "passed": true}\'\nexit 0\n',
            "missed.sh": '#!/bin/bash\necho \'{"score": 0.0, "passed": false}\'\nexit 1\n',
        }
        task_dir = _make_rubric_task(tmp_path, "rubric-partial", criteria, verifiers)
        result = OracleChecksScorer().score("output", task_dir)

        assert result.scorer_family == "oracle_checks"
        assert result.score == pytest.approx(0.6)
        assert result.sub_scores["criterion_scores"] == {
            "covered": pytest.approx(1.0),
            "missed": pytest.approx(0.0),
        }

    def test_all_fail(self, tmp_path: Path) -> None:
        from codeprobe.core.scoring import OracleChecksScorer

        criteria = [
            {"name": "a", "weight": 0.5, "verifier": "a.sh"},
            {"name": "b", "weight": 0.5, "verifier": "b.sh"},
        ]
        verifiers = {
            "a.sh": '#!/bin/bash\necho \'{"score": 0.0, "passed": false}\'\nexit 1\n',
            "b.sh": '#!/bin/bash\necho \'{"score": 0.0, "passed": false}\'\nexit 1\n',
        }
        task_dir = _make_rubric_task(tmp_path, "rubric-all-fail", criteria, verifiers)
        result = OracleChecksScorer().score("output", task_dir)

        assert result.scorer_family == "oracle_checks"
        assert result.score == pytest.approx(0.0)
        assert result.passed is False

    def test_weight_normalization_does_not_require_sum_to_one(
        self, tmp_path: Path
    ) -> None:
        """A rubric with weights ``[2, 1, 1]`` must score the same as
        ``[0.5, 0.25, 0.25]`` — that is the family's defining property
        (`Σ(w·s) / Σ w`). CheckpointScorer rejects this; oracle_checks
        accepts it.
        """
        from codeprobe.core.scoring import OracleChecksScorer

        criteria = [
            {"name": "high", "weight": 2.0, "verifier": "high.sh"},
            {"name": "mid", "weight": 1.0, "verifier": "mid.sh"},
            {"name": "low", "weight": 1.0, "verifier": "low.sh"},
        ]
        verifiers = {
            "high.sh": '#!/bin/bash\necho \'{"score": 1.0, "passed": true}\'\nexit 0\n',
            "mid.sh": '#!/bin/bash\necho \'{"score": 0.0, "passed": false}\'\nexit 1\n',
            "low.sh": '#!/bin/bash\necho \'{"score": 0.0, "passed": false}\'\nexit 1\n',
        }
        task_dir = _make_rubric_task(tmp_path, "rubric-norm", criteria, verifiers)
        result = OracleChecksScorer().score("output", task_dir)

        # 2/(2+1+1) = 0.5 — high passed, mid + low failed
        assert result.score == pytest.approx(0.5)
        assert result.sub_scores["total_weight"] == pytest.approx(4.0)

    def test_metadata_criteria_take_precedence_over_rubric_json(
        self, tmp_path: Path
    ) -> None:
        """When ``metadata_criteria`` is passed at construction, the
        on-disk ``tests/rubric.json`` MUST be ignored. Mirrors the
        precedence in :class:`CheckpointScorer`."""
        from codeprobe.core.scoring import OracleChecksScorer

        on_disk = [
            {"name": "stale", "weight": 1.0, "verifier": "stale.sh"},
        ]
        metadata = [
            {"name": "fresh", "weight": 1.0, "verifier": "fresh.sh"},
        ]
        verifiers = {
            "fresh.sh": '#!/bin/bash\necho \'{"score": 1.0, "passed": true}\'\nexit 0\n',
        }
        task_dir = _make_rubric_task(tmp_path, "rubric-metadata", on_disk, verifiers)
        result = OracleChecksScorer(metadata_criteria=metadata).score("output", task_dir)

        assert result.scorer_family == "oracle_checks"
        assert result.score == pytest.approx(1.0)
        assert "fresh" in result.sub_scores["criterion_scores"]
        assert "stale" not in result.sub_scores["criterion_scores"]

    def test_missing_rubric_json_is_loud_error(self, tmp_path: Path) -> None:
        from codeprobe.core.scoring import OracleChecksScorer

        task_dir = tmp_path / "rubric-missing"
        (task_dir / "tests").mkdir(parents=True)
        result = OracleChecksScorer().score("output", task_dir)

        assert result.scorer_family == "oracle_checks"
        assert result.score == pytest.approx(0.0)
        assert result.passed is False
        assert result.error is not None
        assert "rubric.json" in result.error

    def test_missing_verifier_script_is_loud_error(self, tmp_path: Path) -> None:
        from codeprobe.core.scoring import OracleChecksScorer

        criteria = [
            {"name": "ghost", "weight": 1.0, "verifier": "does_not_exist.sh"},
        ]
        task_dir = _make_rubric_task(tmp_path, "rubric-no-verifier", criteria, {})
        result = OracleChecksScorer().score("output", task_dir)

        assert result.scorer_family == "oracle_checks"
        assert result.score == pytest.approx(0.0)
        assert result.error is not None
        assert "Verifier not found" in result.error

    def test_missing_verifier_field_is_loud_error(self, tmp_path: Path) -> None:
        from codeprobe.core.scoring import OracleChecksScorer

        criteria = [{"name": "no_verifier", "weight": 1.0}]
        task_dir = _make_rubric_task(tmp_path, "rubric-no-verifier-field", criteria, {})
        result = OracleChecksScorer().score("output", task_dir)

        assert result.scorer_family == "oracle_checks"
        assert result.error is not None
        assert "verifier" in result.error.lower()

    def test_negative_weight_is_loud_error(self, tmp_path: Path) -> None:
        from codeprobe.core.scoring import OracleChecksScorer

        criteria = [{"name": "bad", "weight": -1.0, "verifier": "x.sh"}]
        task_dir = _make_rubric_task(tmp_path, "rubric-neg-weight", criteria, {})
        result = OracleChecksScorer().score("output", task_dir)

        assert result.scorer_family == "oracle_checks"
        assert result.error is not None
        assert "non-negative" in result.error.lower()

    def test_zero_total_weight_is_loud_error(self, tmp_path: Path) -> None:
        from codeprobe.core.scoring import OracleChecksScorer

        criteria = [
            {"name": "a", "weight": 0.0, "verifier": "a.sh"},
            {"name": "b", "weight": 0.0, "verifier": "b.sh"},
        ]
        task_dir = _make_rubric_task(tmp_path, "rubric-zero-weight", criteria, {})
        result = OracleChecksScorer().score("output", task_dir)

        assert result.scorer_family == "oracle_checks"
        assert result.error is not None
        assert "weights sum to zero" in result.error.lower()

    def test_empty_criteria_list_is_loud_error(self, tmp_path: Path) -> None:
        from codeprobe.core.scoring import OracleChecksScorer

        task_dir = _make_rubric_task(tmp_path, "rubric-empty", [], {})
        result = OracleChecksScorer().score("output", task_dir)

        assert result.scorer_family == "oracle_checks"
        assert result.error is not None
        assert "at least one criterion" in result.error.lower()

    def test_malformed_rubric_json_is_loud_error(self, tmp_path: Path) -> None:
        from codeprobe.core.scoring import OracleChecksScorer

        task_dir = tmp_path / "rubric-bad-json"
        (task_dir / "tests").mkdir(parents=True)
        (task_dir / "tests" / "rubric.json").write_text(
            "not valid json", encoding="utf-8"
        )
        result = OracleChecksScorer().score("output", task_dir)

        assert result.scorer_family == "oracle_checks"
        assert result.error is not None
        assert "rubric.json" in result.error.lower()

    def test_rubric_json_must_be_a_list(self, tmp_path: Path) -> None:
        from codeprobe.core.scoring import OracleChecksScorer

        task_dir = tmp_path / "rubric-not-list"
        (task_dir / "tests").mkdir(parents=True)
        (task_dir / "tests" / "rubric.json").write_text(
            json.dumps({"criteria": []}), encoding="utf-8"
        )
        result = OracleChecksScorer().score("output", task_dir)

        assert result.scorer_family == "oracle_checks"
        assert result.error is not None
        assert "list" in result.error.lower()

    def test_verifier_exit_zero_with_no_stdout_is_full_pass(
        self, tmp_path: Path
    ) -> None:
        """Documented fallback: exit 0 → 1.0, exit nonzero → 0.0 when
        the verifier emits no JSON."""
        from codeprobe.core.scoring import OracleChecksScorer

        criteria = [{"name": "silent", "weight": 1.0, "verifier": "silent.sh"}]
        verifiers = {"silent.sh": "#!/bin/bash\nexit 0\n"}
        task_dir = _make_rubric_task(tmp_path, "rubric-silent", criteria, verifiers)
        result = OracleChecksScorer().score("output", task_dir)

        assert result.scorer_family == "oracle_checks"
        assert result.score == pytest.approx(1.0)

    def test_registry_resolves_oracle_checks(self) -> None:
        """``get_scorer("oracle_checks")`` returns an :class:`OracleChecksScorer`."""
        from codeprobe.core.registry import resolve_scorer
        from codeprobe.core.scoring import OracleChecksScorer

        scorer = resolve_scorer("oracle_checks")
        assert isinstance(scorer, OracleChecksScorer)

    def test_fixture_task_scores_under_oracle_checks(self) -> None:
        """The shipped ``tests/fixtures/oracle_checks_task`` fixture must
        score cleanly. Acceptance A5: a fixture using the new family is
        added to demonstrate end-to-end wiring."""
        from codeprobe.core.scoring import OracleChecksScorer

        fixture_dir = (
            Path(__file__).parent / "fixtures" / "oracle_checks_task"
        )
        assert fixture_dir.is_dir(), f"missing fixture: {fixture_dir}"
        assert (fixture_dir / "tests" / "rubric.json").is_file()

        # Agent output that satisfies all three rubric criteria.
        good_output = (
            "Implementation handles edge_case_x gracefully.\n"
            "All error branches covered:\n"
            "  raise ValueError on invalid input\n"
            "  except FileNotFoundError\n"
            "  return error code on overflow\n"
            "Public API surface preserved.\n"
        )
        result = OracleChecksScorer().score(good_output, fixture_dir)
        assert result.scorer_family == "oracle_checks"
        assert result.score == pytest.approx(1.0)
        assert result.sub_scores["criterion_scores"] == {
            "handles_edge_case_x": pytest.approx(1.0),
            "covers_error_branches": pytest.approx(1.0),
            "preserves_public_api": pytest.approx(1.0),
        }


# ---------------------------------------------------------------------------
# Aggregate schema — mean_reward + ir_diagnostics block (back-compat)
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
            automated_score=0.40,
            duration_seconds=1.0,
            cost_usd=0.01,
            scoring_details={
                "passed": False,
                "precision": 0.25,
                "recall": 1.0,
                "f1": 0.40,
                "scorer_family": "oracle_overlap_f1",
            },
        ),
        CompletedTask(
            task_id="t2",
            automated_score=0.667,
            duration_seconds=1.0,
            cost_usd=0.01,
            scoring_details={
                "passed": True,
                "precision": 1.0,
                "recall": 0.5,
                "f1": 2 / 3,
                "scorer_family": "oracle_overlap_f1",
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
    assert summary["mean_reward"] == pytest.approx((0.40 + 0.667) / 2)
    assert summary["mean_automated_score"] == pytest.approx((0.40 + 0.667) / 2)
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


def test_score_result_carries_full_contract() -> None:
    """A1 acceptance: every IR ScoreResult has reward + scorer_family +
    sub_scores + diagnostics with ir_metrics."""
    result = score_file_list(["a.py"], ["a.py"])
    # reward (mirrored on score and reward_score)
    assert isinstance(result.score, float)
    assert result.reward_score == result.score
    # scorer_family — registered
    assert result.scorer_family in SCORER_FAMILIES
    # sub_scores — rubric breakdown
    assert isinstance(result.sub_scores, dict)
    assert "reward" in result.sub_scores
    # diagnostics — IR view
    assert "ir_metrics" in result.diagnostics
    assert "f1" in result.diagnostics["ir_metrics"]


__all__ = [
    "TestScoreFileListDefaultFamilyIsF1",
    "TestScoreFileListRecallFamily",
    "TestScoreSymbolListFamily",
    "TestUr8dHonestScoreSurvives",
    "TestContinuousScorerFamilyRouting",
    "TestArtifactScorerFamilyRouting",
    "TestExactMatchFamily",
    "TestSequenceLcsFamily",
    "TestBinaryTestFamily",
    "TestOracleChecksFamily",
    "test_aggregate_emits_mean_reward_and_ir_diagnostics",
    "test_score_result_carries_full_contract",
]
