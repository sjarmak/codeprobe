"""Unit tests for :mod:`codeprobe.mining.consensus`.

The runner orchestrates several backends through opaque function calls;
the unit-tested surface here is purely mechanical (combination, pairwise
F1, decision boundary, divergence-report shape). Backend resolvers
themselves are tested via monkey-patching so the test never depends on
``rg``, the AST scanner, or Sourcegraph being available.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from codeprobe.mining import consensus
from codeprobe.mining.consensus import (
    DEFAULT_BACKENDS,
    BackendResult,
    ConsensusDecision,
    _combine_files,
    _pairwise_metrics,
    compute_consensus,
    parse_backend_list,
)


# ---------------------------------------------------------------------------
# parse_backend_list — CLI parser
# ---------------------------------------------------------------------------


class TestParseBackendList:
    def test_empty_returns_default(self) -> None:
        assert parse_backend_list("") == DEFAULT_BACKENDS
        assert parse_backend_list(None) == DEFAULT_BACKENDS

    def test_single_backend(self) -> None:
        assert parse_backend_list("ast") == ("ast",)

    def test_multiple_backends_preserve_order(self) -> None:
        assert parse_backend_list("grep,ast") == ("grep", "ast")
        assert parse_backend_list("ast,sourcegraph") == ("ast", "sourcegraph")

    def test_dedup_preserves_first_occurrence(self) -> None:
        assert parse_backend_list("ast,grep,ast") == ("ast", "grep")

    def test_whitespace_tolerated(self) -> None:
        assert parse_backend_list("ast , grep") == ("ast", "grep")

    def test_unknown_backend_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown consensus backend"):
            parse_backend_list("ast,bogus")

    def test_empty_after_strip_returns_default(self) -> None:
        assert parse_backend_list(" , ") == DEFAULT_BACKENDS


# ---------------------------------------------------------------------------
# _combine_files — intersection vs. union
# ---------------------------------------------------------------------------


class TestCombineFiles:
    def _br(self, name: str, files: set[str]) -> BackendResult:
        return BackendResult(backend=name, files=frozenset(files))  # type: ignore[arg-type]

    def test_intersection_two_backends(self) -> None:
        a = self._br("ast", {"a.go", "b.go"})
        b = self._br("grep", {"b.go", "c.go"})
        out = _combine_files((a, b), "intersection")
        assert out == frozenset({"b.go"})

    def test_union_two_backends(self) -> None:
        a = self._br("ast", {"a.go", "b.go"})
        b = self._br("grep", {"b.go", "c.go"})
        out = _combine_files((a, b), "union")
        assert out == frozenset({"a.go", "b.go", "c.go"})

    def test_intersection_three_backends(self) -> None:
        a = self._br("ast", {"a.go", "b.go", "c.go"})
        b = self._br("grep", {"b.go", "c.go", "d.go"})
        c = self._br("sourcegraph", {"b.go", "c.go", "x.go"})
        assert _combine_files((a, b, c), "intersection") == frozenset(
            {"b.go", "c.go"}
        )

    def test_empty_input_returns_empty(self) -> None:
        assert _combine_files((), "intersection") == frozenset()
        assert _combine_files((), "union") == frozenset()


# ---------------------------------------------------------------------------
# _pairwise_metrics
# ---------------------------------------------------------------------------


class TestPairwiseMetrics:
    def _br(self, name: str, files: set[str]) -> BackendResult:
        return BackendResult(backend=name, files=frozenset(files))  # type: ignore[arg-type]

    def test_perfect_agreement(self) -> None:
        a = self._br("ast", {"a.go", "b.go"})
        b = self._br("grep", {"a.go", "b.go"})
        pairs, min_f1, max_f1 = _pairwise_metrics((a, b))
        assert min_f1 == 1.0
        assert max_f1 == 1.0
        assert pairs[0]["f1"] == 1.0

    def test_total_disagreement(self) -> None:
        a = self._br("ast", {"a.go"})
        b = self._br("grep", {"b.go"})
        pairs, min_f1, max_f1 = _pairwise_metrics((a, b))
        assert min_f1 == 0.0
        assert max_f1 == 0.0
        # Divergence members exposed for the report
        assert pairs[0]["ast_only"] == ["a.go"]
        assert pairs[0]["grep_only"] == ["b.go"]

    def test_partial_overlap_metrics(self) -> None:
        a = self._br("ast", {"a", "b", "c"})
        b = self._br("grep", {"b", "c", "d"})
        pairs, min_f1, max_f1 = _pairwise_metrics((a, b))
        # F1 = 2*2/(3+3) = 0.6667
        assert pairs[0]["f1"] == pytest.approx(0.6667, abs=1e-3)
        assert min_f1 == max_f1 == pairs[0]["f1"]

    def test_three_backends_three_pairs(self) -> None:
        a = self._br("ast", {"a"})
        b = self._br("grep", {"a"})
        c = self._br("sourcegraph", {"b"})
        pairs, _min_f1, max_f1 = _pairwise_metrics((a, b, c))
        assert len(pairs) == 3  # C(3,2)
        # ast vs grep is perfect; max F1 across pairs is 1.0
        assert max_f1 == 1.0

    def test_single_backend_no_pairs(self) -> None:
        a = self._br("ast", {"a"})
        pairs, min_f1, max_f1 = _pairwise_metrics((a,))
        assert pairs == []
        assert min_f1 == 0.0
        assert max_f1 == 0.0


# ---------------------------------------------------------------------------
# compute_consensus — full integration with monkey-patched backends
# ---------------------------------------------------------------------------


def _patch_backends(
    monkeypatch: pytest.MonkeyPatch,
    *,
    grep: set[str] | None = None,
    ast: set[str] | None = None,  # noqa: A002 - test fixture name
    sg: set[str] | None = None,
    grep_unavailable: bool = False,
    ast_unavailable: bool = False,
    sg_unavailable: bool = False,
) -> None:
    """Replace the three backend dispatchers with deterministic stubs."""

    def _fake_grep(symbol: str, repo_paths: Any) -> BackendResult:
        if grep_unavailable:
            return BackendResult(
                backend="grep", available=False, error="stubbed unavailable"
            )
        return BackendResult(backend="grep", files=frozenset(grep or set()))

    def _fake_ast(symbol: str, repo_paths: Any, *, defining_file: str = "") -> BackendResult:
        if ast_unavailable:
            return BackendResult(
                backend="ast", available=False, error="stubbed unavailable"
            )
        return BackendResult(backend="ast", files=frozenset(ast or set()))

    def _fake_sg(
        symbol: str,
        repo_paths: Any,
        *,
        defining_file: str,
        sg_repo: str,
        sg_url: str = "",
    ) -> BackendResult:
        if sg_unavailable:
            return BackendResult(
                backend="sourcegraph",
                available=False,
                error="stubbed unavailable",
            )
        return BackendResult(
            backend="sourcegraph", files=frozenset(sg or set())
        )

    monkeypatch.setattr(consensus, "_run_grep_backend", _fake_grep)
    monkeypatch.setattr(consensus, "_run_ast_backend", _fake_ast)
    monkeypatch.setattr(consensus, "_run_sourcegraph_backend", _fake_sg)


class TestComputeConsensus:
    @pytest.fixture(autouse=True)
    def _common_repo_paths(self, tmp_path: Path) -> None:
        self.repo_paths = [tmp_path]

    def test_perfect_agreement_ships(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_backends(
            monkeypatch,
            grep={"a.go", "b.go"},
            ast={"a.go", "b.go"},
            sg={"a.go", "b.go"},
        )
        decision = compute_consensus(
            symbol="MyFunc",
            defining_file="pkg/foo.go",
            repo_paths=self.repo_paths,
            sg_repo="github.com/x/y",
            threshold=0.8,
        )
        assert decision.shipped is True
        assert decision.consensus_files == frozenset({"a.go", "b.go"})
        assert decision.max_pair_f1 == 1.0
        assert set(decision.available_backends) == {
            "grep",
            "ast",
            "sourcegraph",
        }
        # Divergence report is shipped alongside even when consensus passes
        assert decision.divergence_report["decision"] == "shipped"
        assert decision.divergence_report["threshold"] == 0.8
        assert decision.divergence_report["mode"] == "intersection"

    def test_below_threshold_quarantines(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_backends(
            monkeypatch,
            grep={"a.go", "b.go", "c.go", "d.go"},
            ast={"x.go", "y.go"},
            sg={"q.go"},
        )
        decision = compute_consensus(
            symbol="DivergentFunc",
            defining_file="pkg/foo.go",
            repo_paths=self.repo_paths,
            sg_repo="github.com/x/y",
            threshold=0.8,
        )
        assert decision.shipped is False
        # max_pair_f1 across {grep,ast,sg} is 0.0 — totally disjoint sets
        assert decision.max_pair_f1 == 0.0
        assert decision.divergence_report["decision"] == "quarantined"

    def test_intersection_mode_drops_unique_files(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # All three overlap on b.go but each contributes uniques.
        _patch_backends(
            monkeypatch,
            grep={"a.go", "b.go"},
            ast={"b.go", "c.go"},
            sg={"b.go", "d.go"},
        )
        decision = compute_consensus(
            symbol="X",
            defining_file="pkg/foo.go",
            repo_paths=self.repo_paths,
            sg_repo="github.com/x/y",
            threshold=0.5,  # 2/(2+2)=0.5 between any two backends — borderline
            mode="intersection",
        )
        # Intersection across grep/ast/sg = {b.go}
        assert decision.consensus_files == frozenset({"b.go"})

    def test_union_mode_keeps_all_files(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_backends(
            monkeypatch,
            grep={"a.go", "b.go"},
            ast={"b.go", "c.go"},
            sg={"b.go", "d.go"},
        )
        decision = compute_consensus(
            symbol="X",
            defining_file="pkg/foo.go",
            repo_paths=self.repo_paths,
            sg_repo="github.com/x/y",
            threshold=0.5,
            mode="union",
        )
        assert decision.consensus_files == frozenset(
            {"a.go", "b.go", "c.go", "d.go"}
        )

    def test_one_backend_unavailable_still_compares_remaining(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_backends(
            monkeypatch,
            grep={"a.go"},
            ast={"a.go"},
            sg=None,
            sg_unavailable=True,
        )
        decision = compute_consensus(
            symbol="X",
            defining_file="pkg/foo.go",
            repo_paths=self.repo_paths,
            sg_repo="github.com/x/y",
            threshold=0.8,
        )
        # SG is unavailable; ast and grep agree perfectly → ship.
        assert decision.shipped is True
        assert "sourcegraph" not in decision.available_backends
        assert set(decision.available_backends) == {"grep", "ast"}

    def test_only_one_backend_available_quarantines(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_backends(
            monkeypatch,
            grep={"a.go"},
            ast=None,
            sg=None,
            ast_unavailable=True,
            sg_unavailable=True,
        )
        decision = compute_consensus(
            symbol="X",
            defining_file="pkg/foo.go",
            repo_paths=self.repo_paths,
            sg_repo="github.com/x/y",
            threshold=0.8,
        )
        # Single-backend candidates cannot cross the consensus gate.
        assert decision.shipped is False

    def test_invalid_threshold_raises(self) -> None:
        with pytest.raises(ValueError, match="threshold"):
            compute_consensus(
                symbol="X",
                defining_file="pkg/foo.go",
                repo_paths=[Path(".")],
                threshold=1.5,
            )

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ValueError, match="mode"):
            compute_consensus(
                symbol="X",
                defining_file="pkg/foo.go",
                repo_paths=[Path(".")],
                mode="diff",  # type: ignore[arg-type]
            )

    def test_empty_symbol_raises(self) -> None:
        with pytest.raises(ValueError, match="symbol"):
            compute_consensus(
                symbol="",
                defining_file="pkg/foo.go",
                repo_paths=[Path(".")],
            )

    def test_divergence_report_contains_per_backend_files(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_backends(
            monkeypatch,
            grep={"x.go"},
            ast={"y.go"},
            sg={"z.go"},
        )
        decision = compute_consensus(
            symbol="X",
            defining_file="pkg/foo.go",
            repo_paths=[Path(".")],
            sg_repo="github.com/x/y",
            threshold=0.8,
        )
        rep = decision.divergence_report
        # Each attempted backend gets its own row in the report, even
        # when the backend was available — so reviewers see the full
        # state. (Unavailable backends still appear with available=False.)
        assert {br["backend"] for br in rep["backend_results"]} == {
            "grep",
            "ast",
            "sourcegraph",
        }
        for br in rep["backend_results"]:
            if br["backend"] == "grep":
                assert br["files"] == ["x.go"]
            if br["backend"] == "ast":
                assert br["files"] == ["y.go"]
            if br["backend"] == "sourcegraph":
                assert br["files"] == ["z.go"]
        assert rep["pair_metrics"]  # at least one pair recorded
        assert rep["consensus_files"] == []  # intersection of disjoint sets

    def test_decision_is_immutable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_backends(monkeypatch, grep={"a"}, ast={"a"}, sg={"a"})
        decision = compute_consensus(
            symbol="X",
            defining_file="pkg/foo.go",
            repo_paths=[Path(".")],
            sg_repo="github.com/x/y",
        )
        assert isinstance(decision, ConsensusDecision)
        with pytest.raises((TypeError, AttributeError, Exception)):
            decision.shipped = False  # type: ignore[misc]
