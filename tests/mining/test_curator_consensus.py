"""Unit tests for :mod:`codeprobe.mining.oracle_curator`.

Covers the four scenarios called out in codeprobe-zat9:

* 2-backend agreement → tier-1 ``required``
* 3-backend agreement / majority → tier-1 ``required``
* LLM-resolved single-backend disagreement → tier-2 ``supplementary``
* Single-backend fallback → keep all as ``required``, no LLM call

Plus a few edge cases (LLM rejection, LLM unavailable, no backends
available) so the tier-2 quarantine path is covered.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.mining import oracle_curator
from codeprobe.mining.consensus import BackendResult
from codeprobe.mining.oracle_curator import (
    CuratedItem,
    CuratedOracle,
    CuratorVote,
    curate_consensus,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _br(
    backend: str,
    files: Iterable[str],
    *,
    available: bool = True,
) -> BackendResult:
    return BackendResult(
        backend=backend,  # type: ignore[arg-type]
        files=frozenset(files),
        available=available,
    )


def _by_path(items: tuple[CuratedItem, ...]) -> dict[str, CuratedItem]:
    return {it.path: it for it in items}


# ---------------------------------------------------------------------------
# Tier-1 — multi-backend agreement
# ---------------------------------------------------------------------------


class TestTwoBackendAgreement:
    """All files reported by both backends → required, no LLM."""

    def test_keeps_all_as_required(self, tmp_path: Path) -> None:
        results = [
            _br("grep", ["a.py", "b.py"]),
            _br("ast", ["a.py", "b.py"]),
        ]
        out = curate_consensus(
            backend_results=results,
            symbol="Foo",
            defining_file="src/foo.py",
            repo_paths=[tmp_path],
            use_llm=False,
        )

        assert {it.path for it in out.items} == {"a.py", "b.py"}
        assert all(it.tier == "required" for it in out.items)
        assert all(
            it.backends == ("ast", "grep") for it in out.items
        )
        assert all(not it.via_llm_review for it in out.items)
        assert out.backends_consensus == ("ast", "grep")
        assert out.quarantined == ()
        assert out.llm_used is False

    def test_one_file_unique_to_one_backend_quarantined_when_no_llm(
        self, tmp_path: Path
    ) -> None:
        results = [
            _br("grep", ["a.py", "b.py"]),
            _br("ast", ["a.py"]),
        ]
        out = curate_consensus(
            backend_results=results,
            symbol="Foo",
            defining_file="src/foo.py",
            repo_paths=[tmp_path],
            use_llm=False,
        )

        items = _by_path(out.items)
        assert "a.py" in items
        assert items["a.py"].tier == "required"
        assert "b.py" not in items
        assert any("b.py" == p for p, _ in out.quarantined)


# ---------------------------------------------------------------------------
# Tier-1 — three-backend majority
# ---------------------------------------------------------------------------


class TestThreeBackendAgreement:
    """Files agreed by >= 2 of 3 backends are required."""

    def test_three_way_full_agreement(self, tmp_path: Path) -> None:
        results = [
            _br("grep", ["a.py"]),
            _br("ast", ["a.py"]),
            _br("sourcegraph", ["a.py"]),
        ]
        out = curate_consensus(
            backend_results=results,
            symbol="Foo",
            defining_file="src/foo.py",
            repo_paths=[tmp_path],
            use_llm=False,
        )

        assert len(out.items) == 1
        only = out.items[0]
        assert only.path == "a.py"
        assert only.tier == "required"
        assert set(only.backends) == {"grep", "ast", "sourcegraph"}
        assert set(out.backends_consensus) == {
            "grep",
            "ast",
            "sourcegraph",
        }

    def test_two_of_three_majority_keeps_required(
        self, tmp_path: Path
    ) -> None:
        results = [
            _br("grep", ["a.py", "b.py"]),
            _br("ast", ["a.py", "b.py"]),
            _br("sourcegraph", []),  # backend ran but found nothing
        ]
        out = curate_consensus(
            backend_results=results,
            symbol="Foo",
            defining_file="src/foo.py",
            repo_paths=[tmp_path],
            use_llm=False,
        )

        items = _by_path(out.items)
        assert set(items) == {"a.py", "b.py"}
        for it in items.values():
            assert it.tier == "required"
            assert set(it.backends) == {"grep", "ast"}
        # sourcegraph contributed no kept items, so it's not in the
        # consensus tuple even though it was available.
        assert set(out.backends_consensus) == {"grep", "ast"}


# ---------------------------------------------------------------------------
# Tier-2 — LLM-resolved single-backend disagreement
# ---------------------------------------------------------------------------


class TestLLMResolvedDisagreement:
    """Files reported by exactly one backend → LLM curator vote."""

    def test_llm_keep_promotes_to_supplementary(
        self, tmp_path: Path
    ) -> None:
        results = [
            _br("grep", ["a.py", "b.py"]),
            _br("ast", ["a.py"]),
            _br("sourcegraph", ["a.py"]),
        ]
        # Make b.py readable so the curator's snippet read succeeds.
        (tmp_path / "b.py").write_text("from src.foo import Foo\n")

        with patch.object(
            oracle_curator,
            "_curate_with_llm",
            return_value=CuratorVote(
                keep=True, rationale="aliased import of Foo"
            ),
        ) as mock_llm:
            out = curate_consensus(
                backend_results=results,
                symbol="Foo",
                defining_file="src/foo.py",
                repo_paths=[tmp_path],
                use_llm=True,
            )

        items = _by_path(out.items)
        assert items["a.py"].tier == "required"
        assert set(items["a.py"].backends) == {
            "grep",
            "ast",
            "sourcegraph",
        }
        assert items["b.py"].tier == "supplementary"
        assert items["b.py"].via_llm_review is True
        assert items["b.py"].backends == ("grep",)
        assert items["b.py"].llm_rationale == "aliased import of Foo"
        # The LLM curator was called exactly once (only b.py is tier-2).
        assert mock_llm.call_count == 1
        assert out.llm_used is True

    def test_llm_reject_quarantines(self, tmp_path: Path) -> None:
        results = [
            _br("grep", ["a.py", "spurious.py"]),
            _br("ast", ["a.py"]),
        ]

        with patch.object(
            oracle_curator,
            "_curate_with_llm",
            return_value=CuratorVote(
                keep=False, rationale="unrelated mention in a comment"
            ),
        ):
            out = curate_consensus(
                backend_results=results,
                symbol="Foo",
                defining_file="src/foo.py",
                repo_paths=[tmp_path],
                use_llm=True,
            )

        assert {it.path for it in out.items} == {"a.py"}
        assert out.items[0].tier == "required"
        assert any(p == "spurious.py" for p, _ in out.quarantined)
        reasons = {p: r for p, r in out.quarantined}
        assert "LLM rejected" in reasons["spurious.py"]
        assert out.llm_used is True

    def test_llm_error_quarantines_with_reason(
        self, tmp_path: Path
    ) -> None:
        results = [
            _br("grep", ["a.py", "b.py"]),
            _br("ast", ["a.py"]),
        ]
        with patch.object(
            oracle_curator,
            "_curate_with_llm",
            return_value=CuratorVote(
                keep=False, error="non-JSON response"
            ),
        ):
            out = curate_consensus(
                backend_results=results,
                symbol="Foo",
                defining_file="src/foo.py",
                repo_paths=[tmp_path],
                use_llm=True,
            )

        reasons = {p: r for p, r in out.quarantined}
        assert "b.py" in reasons
        assert "LLM error" in reasons["b.py"]


# ---------------------------------------------------------------------------
# Single-backend fallback
# ---------------------------------------------------------------------------


class TestSingleBackendFallback:
    """One backend → no consensus filter, no LLM call, all required."""

    def test_keeps_all_as_required_no_llm_call(
        self, tmp_path: Path
    ) -> None:
        results = [_br("grep", ["a.py", "b.py", "c.py"])]
        with patch.object(
            oracle_curator, "_curate_with_llm"
        ) as mock_llm:
            out = curate_consensus(
                backend_results=results,
                symbol="Foo",
                defining_file="src/foo.py",
                repo_paths=[tmp_path],
                use_llm=True,
            )

        assert {it.path for it in out.items} == {
            "a.py",
            "b.py",
            "c.py",
        }
        for it in out.items:
            assert it.tier == "required"
            assert it.backends == ("grep",)
            assert not it.via_llm_review
        assert out.backends_consensus == ("grep",)
        assert out.quarantined == ()
        assert out.llm_used is False
        mock_llm.assert_not_called()

    def test_unavailable_backends_dont_count_for_fallback(
        self, tmp_path: Path
    ) -> None:
        # Two backends configured but only one available — should still
        # take the single-backend fallback path.
        results = [
            _br("grep", ["a.py"]),
            _br(
                "sourcegraph",
                [],
                available=False,
            ),
        ]
        out = curate_consensus(
            backend_results=results,
            symbol="Foo",
            defining_file="src/foo.py",
            repo_paths=[tmp_path],
            use_llm=False,
        )
        assert out.backends_consensus == ("grep",)
        assert all(it.tier == "required" for it in out.items)


# ---------------------------------------------------------------------------
# LLM unavailable / no backends
# ---------------------------------------------------------------------------


class TestLLMUnavailable:
    """When the LLM is unavailable, tier-2 candidates are quarantined."""

    def test_llm_unavailable_quarantines_tier2(
        self, tmp_path: Path
    ) -> None:
        results = [
            _br("grep", ["a.py", "b.py"]),
            _br("ast", ["a.py"]),
        ]
        with patch.object(
            oracle_curator, "llm_available", return_value=False
        ):
            out = curate_consensus(
                backend_results=results,
                symbol="Foo",
                defining_file="src/foo.py",
                repo_paths=[tmp_path],
                use_llm=True,
            )

        assert {it.path for it in out.items} == {"a.py"}
        reasons = {p: r for p, r in out.quarantined}
        assert "b.py" in reasons
        assert "LLM unavailable" in reasons["b.py"]
        assert out.llm_used is False

    def test_use_llm_false_quarantines_tier2(
        self, tmp_path: Path
    ) -> None:
        # Even when llm_available() is True, opting out via use_llm=False
        # must take the same conservative quarantine path.
        results = [
            _br("grep", ["a.py", "b.py"]),
            _br("ast", ["a.py"]),
        ]
        with patch.object(
            oracle_curator, "llm_available", return_value=True
        ):
            out = curate_consensus(
                backend_results=results,
                symbol="Foo",
                defining_file="src/foo.py",
                repo_paths=[tmp_path],
                use_llm=False,
            )

        assert {it.path for it in out.items} == {"a.py"}


class TestNoBackendsAvailable:
    def test_returns_empty_oracle(self, tmp_path: Path) -> None:
        results = [
            _br("grep", ["a.py"], available=False),
            _br("ast", ["b.py"], available=False),
        ]
        out = curate_consensus(
            backend_results=results,
            symbol="Foo",
            defining_file="src/foo.py",
            repo_paths=[tmp_path],
            use_llm=True,
        )
        assert out.items == ()
        assert out.backends_consensus == ()
        assert out.quarantined == ()


class TestMinBackendsValidation:
    def test_min_backends_zero_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="min_backends must be"):
            curate_consensus(
                backend_results=[_br("grep", ["a.py"])],
                symbol="Foo",
                defining_file="src/foo.py",
                repo_paths=[tmp_path],
                min_backends=0,
            )

    def test_min_backends_three_requires_three_way_agreement(
        self, tmp_path: Path
    ) -> None:
        results = [
            _br("grep", ["a.py", "b.py"]),
            _br("ast", ["a.py", "b.py"]),
            _br("sourcegraph", ["a.py"]),
        ]
        # Stub LLM to keep only b.py's curated path predictable.
        with patch.object(
            oracle_curator,
            "_curate_with_llm",
            return_value=CuratorVote(keep=False),
        ):
            out = curate_consensus(
                backend_results=results,
                symbol="Foo",
                defining_file="src/foo.py",
                repo_paths=[tmp_path],
                min_backends=3,
                use_llm=True,
            )

        # Only a.py was found by all three backends.
        assert {it.path for it in out.items} == {"a.py"}


# ---------------------------------------------------------------------------
# Snippet reader
# ---------------------------------------------------------------------------


class TestReadSnippet:
    def test_returns_empty_when_file_missing(
        self, tmp_path: Path
    ) -> None:
        assert (
            oracle_curator._read_snippet([tmp_path], "missing.py") == ""
        )

    def test_caps_lines(self, tmp_path: Path) -> None:
        big = tmp_path / "big.py"
        big.write_text("\n".join(f"# line {i}" for i in range(500)))
        snippet = oracle_curator._read_snippet([tmp_path], "big.py")
        assert snippet.count("\n") < 200

    def test_caps_bytes(self, tmp_path: Path) -> None:
        big = tmp_path / "huge.py"
        big.write_text("x" * 50_000)
        snippet = oracle_curator._read_snippet([tmp_path], "huge.py")
        assert len(snippet) <= oracle_curator._MAX_SNIPPET_BYTES


# ---------------------------------------------------------------------------
# CuratedOracle invariants
# ---------------------------------------------------------------------------


class TestCuratedOracleInvariants:
    def test_items_are_sorted_by_path(self, tmp_path: Path) -> None:
        results = [
            _br("grep", ["zeta.py", "alpha.py", "mid.py"]),
            _br("ast", ["zeta.py", "alpha.py", "mid.py"]),
        ]
        out = curate_consensus(
            backend_results=results,
            symbol="Foo",
            defining_file="src/foo.py",
            repo_paths=[tmp_path],
            use_llm=False,
        )
        paths = [it.path for it in out.items]
        assert paths == sorted(paths)

    def test_backends_consensus_is_sorted(
        self, tmp_path: Path
    ) -> None:
        results = [
            _br("sourcegraph", ["a.py"]),
            _br("ast", ["a.py"]),
            _br("grep", ["a.py"]),
        ]
        out = curate_consensus(
            backend_results=results,
            symbol="Foo",
            defining_file="src/foo.py",
            repo_paths=[tmp_path],
            use_llm=False,
        )
        assert list(out.backends_consensus) == sorted(
            out.backends_consensus
        )

    def test_curated_oracle_is_frozen(self, tmp_path: Path) -> None:
        results = [_br("grep", ["a.py"])]
        out = curate_consensus(
            backend_results=results,
            symbol="Foo",
            defining_file="src/foo.py",
            repo_paths=[tmp_path],
            use_llm=False,
        )
        assert isinstance(out, CuratedOracle)
        with pytest.raises((AttributeError, Exception)):
            out.items = ()  # type: ignore[misc]
