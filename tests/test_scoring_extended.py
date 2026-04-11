"""Tests for extended oracle types: symbol_list and dependency_chain."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.core.scoring import (
    _ORACLE_TYPE_SCORERS,
    ArtifactScorer,
    score_dependency_chain,
    score_symbol_list,
)

# ---------------------------------------------------------------------------
# symbol_list unit tests
# ---------------------------------------------------------------------------


class TestScoreSymbolList:
    def test_exact_match(self) -> None:
        result = score_symbol_list(["Foo", "Bar"], ["Foo", "Bar"])
        assert result.score == 1.0
        assert result.passed is True

    def test_partial_match(self) -> None:
        result = score_symbol_list(["Foo", "Bar", "Baz"], ["Foo", "Bar"])
        assert 0.0 < result.score < 1.0

    def test_no_overlap(self) -> None:
        result = score_symbol_list(["Foo"], ["Bar"])
        assert result.score == 0.0
        assert result.passed is False

    def test_case_insensitive(self) -> None:
        result = score_symbol_list(["MyClass"], ["myclass"])
        assert result.score == 1.0
        assert result.passed is True

    def test_module_prefix_stripping(self) -> None:
        result = score_symbol_list(["foo.bar.MyClass"], ["MyClass"])
        assert result.score == 1.0
        assert result.passed is True

    def test_double_colon_prefix_stripping(self) -> None:
        result = score_symbol_list(["std::vector"], ["vector"])
        assert result.score == 1.0
        assert result.passed is True

    def test_mixed_prefix_and_case(self) -> None:
        result = score_symbol_list(["bar.MyClass"], ["myclass"])
        assert result.score == 1.0
        assert result.passed is True

    def test_empty_expected(self) -> None:
        result = score_symbol_list([], ["Foo"])
        assert result.score == 0.0
        assert result.passed is False

    def test_empty_actual(self) -> None:
        result = score_symbol_list(["Foo"], [])
        assert result.score == 0.0
        assert result.passed is False

    def test_both_empty(self) -> None:
        result = score_symbol_list([], [])
        assert result.score == 0.0
        assert result.passed is False

    def test_non_list_expected(self) -> None:
        result = score_symbol_list("not a list", ["Foo"])
        assert result.score == 0.0
        assert result.passed is False

    def test_non_list_actual(self) -> None:
        result = score_symbol_list(["Foo"], "not a list")
        assert result.score == 0.0
        assert result.passed is False


# ---------------------------------------------------------------------------
# dependency_chain unit tests
# ---------------------------------------------------------------------------


class TestScoreDependencyChain:
    def test_exact_match(self) -> None:
        result = score_dependency_chain(["A", "B", "C"], ["A", "B", "C"])
        assert result.score == 1.0
        assert result.passed is True

    def test_partial_order_match(self) -> None:
        # LCS of ["A","B","C"] and ["A","C"] is ["A","C"] length 2
        # score = 2 / max(3, 2) = 2/3
        result = score_dependency_chain(["A", "B", "C"], ["A", "C"])
        assert result.score == pytest.approx(2.0 / 3.0)

    def test_completely_different(self) -> None:
        result = score_dependency_chain(["A", "B"], ["C", "D"])
        assert result.score == 0.0
        assert result.passed is False

    def test_empty_expected(self) -> None:
        result = score_dependency_chain([], ["A"])
        assert result.score == 0.0
        assert result.passed is False

    def test_empty_actual(self) -> None:
        result = score_dependency_chain(["A"], [])
        assert result.score == 0.0
        assert result.passed is False

    def test_both_empty(self) -> None:
        result = score_dependency_chain([], [])
        assert result.score == 0.0
        assert result.passed is False

    def test_single_element_match(self) -> None:
        result = score_dependency_chain(["A"], ["A"])
        assert result.score == 1.0
        assert result.passed is True

    def test_single_element_mismatch(self) -> None:
        result = score_dependency_chain(["A"], ["B"])
        assert result.score == 0.0
        assert result.passed is False

    def test_case_insensitive(self) -> None:
        result = score_dependency_chain(["Foo", "Bar"], ["foo", "bar"])
        assert result.score == 1.0
        assert result.passed is True

    def test_non_list_expected(self) -> None:
        result = score_dependency_chain("not a list", ["A"])
        assert result.score == 0.0
        assert result.passed is False

    def test_non_list_actual(self) -> None:
        result = score_dependency_chain(["A"], 42)
        assert result.score == 0.0
        assert result.passed is False

    def test_reversed_order(self) -> None:
        # LCS of ["A","B","C"] and ["C","B","A"] is length 1
        # score = 1 / 3
        result = score_dependency_chain(["A", "B", "C"], ["C", "B", "A"])
        assert result.score == pytest.approx(1.0 / 3.0)


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestRegistryIntegration:
    def test_symbol_list_in_registry(self) -> None:
        assert "symbol_list" in _ORACLE_TYPE_SCORERS
        assert _ORACLE_TYPE_SCORERS["symbol_list"] is score_symbol_list

    def test_dependency_chain_in_registry(self) -> None:
        assert "dependency_chain" in _ORACLE_TYPE_SCORERS
        assert _ORACLE_TYPE_SCORERS["dependency_chain"] is score_dependency_chain


# ---------------------------------------------------------------------------
# Full ArtifactScorer integration (end-to-end)
# ---------------------------------------------------------------------------


class TestArtifactScorerExtendedTypes:
    @staticmethod
    def _setup_task(
        task_dir: Path,
        ground_truth: dict,
        answer: dict,
    ) -> None:
        tests_dir = task_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "ground_truth.json").write_text(
            json.dumps(ground_truth), encoding="utf-8"
        )
        (task_dir / "answer.json").write_text(json.dumps(answer), encoding="utf-8")

    def test_symbol_list_e2e(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "task-sym"
        self._setup_task(
            task_dir,
            {"answer_type": "symbol_list", "answer": ["foo.Bar", "baz.Qux"]},
            {"answer": ["Bar", "Qux"]},
        )
        result = ArtifactScorer().score("", task_dir)
        assert result.score == 1.0
        assert result.passed is True

    def test_dependency_chain_e2e(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "task-dep"
        self._setup_task(
            task_dir,
            {"answer_type": "dependency_chain", "answer": ["A", "B", "C"]},
            {"answer": ["A", "B", "C"]},
        )
        result = ArtifactScorer().score("", task_dir)
        assert result.score == 1.0
        assert result.passed is True

    def test_dependency_chain_partial_e2e(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "task-dep-partial"
        self._setup_task(
            task_dir,
            {"answer_type": "dependency_chain", "answer": ["A", "B", "C"]},
            {"answer": ["A", "C"]},
        )
        result = ArtifactScorer().score("", task_dir)
        assert result.score == pytest.approx(2.0 / 3.0)
        assert result.passed is True  # 0.667 >= 0.5
