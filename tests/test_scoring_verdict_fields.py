"""Tests for the ``verdict`` and ``materialized_via`` fields on ``ScoreResult``.

Slice 1a of the hybrid-execution-v1 refactor (bead codeprobe-xysn). These
fields exist independently of the actual diff-materialization behavior
(Slice 1b) — the schema is additive and lands first so call sites can
adopt it incrementally.

See ``docs/prd/prd_hybrid_execution_evaluation.md`` (M7) and
``docs/prd/premortem_hybrid_execution_evaluation.md`` (Theme C, R1).
"""

from __future__ import annotations

import pytest

from codeprobe.core.scoring import (
    ALLOWED_MATERIALIZATION,
    ALLOWED_VERDICTS,
    ScoreResult,
)


class TestVerdictField:
    """``verdict`` distinguishes agent-failure from verifier-infra failure."""

    def test_default_is_none(self) -> None:
        """Legacy callers that don't pass ``verdict`` get ``None``.

        ``None`` means "not migrated yet"; the field is opt-in until every
        scorer call site is updated. Defaulting to ``None`` keeps Slice 1a
        strictly additive.
        """
        result = ScoreResult(score=1.0, passed=True)
        assert result.verdict is None

    @pytest.mark.parametrize(
        "verdict",
        ["correct", "incorrect", "verifier_error", "inconclusive"],
    )
    def test_accepts_each_allowed_value(self, verdict: str) -> None:
        result = ScoreResult(score=0.0, passed=False, verdict=verdict)
        assert result.verdict == verdict

    def test_rejects_unknown_value(self) -> None:
        with pytest.raises(ValueError, match="verdict='maybe' not in"):
            ScoreResult(score=0.5, passed=False, verdict="maybe")

    def test_rejects_empty_string(self) -> None:
        """Empty string is not a valid verdict — must be ``None`` if unset.

        This catches the failure mode where a caller meant to set the
        verdict but stringified ``None`` or an upstream value to ``""``.
        """
        with pytest.raises(ValueError, match="verdict=''"):
            ScoreResult(score=0.5, passed=False, verdict="")

    def test_allowed_set_contents(self) -> None:
        """The canonical set is stable for downstream consumers to import."""
        assert ALLOWED_VERDICTS == frozenset(
            {"correct", "incorrect", "verifier_error", "inconclusive"}
        )


class TestMaterializedViaField:
    """``materialized_via`` records HOW the verifier got the agent's state."""

    def test_default_is_in_place(self) -> None:
        """Legacy default preserves current ``_run_in_sandbox`` behavior.

        Slice 1a does not change scoring behavior — every existing call
        site keeps producing ``materialized_via == "in_place"`` until
        Slice 1b rewrites the scorer to use ``git_apply``.
        """
        result = ScoreResult(score=1.0, passed=True)
        assert result.materialized_via == "in_place"

    @pytest.mark.parametrize(
        "materialization",
        ["in_place", "git_apply", "file_overlay"],
    )
    def test_accepts_each_allowed_value(self, materialization: str) -> None:
        result = ScoreResult(
            score=1.0, passed=True, materialized_via=materialization
        )
        assert result.materialized_via == materialization

    def test_rejects_unknown_value(self) -> None:
        with pytest.raises(ValueError, match="materialized_via='snapshot'"):
            ScoreResult(score=1.0, passed=True, materialized_via="snapshot")

    def test_allowed_set_contents(self) -> None:
        assert ALLOWED_MATERIALIZATION == frozenset(
            {"in_place", "git_apply", "file_overlay"}
        )


class TestBackwardCompat:
    """Existing call sites that don't set the new fields keep working."""

    def test_minimal_construction(self) -> None:
        """A bare ``ScoreResult(score, passed)`` still works."""
        result = ScoreResult(score=0.5, passed=False)
        assert result.score == 0.5
        assert result.passed is False
        assert result.verdict is None
        assert result.materialized_via == "in_place"
        # Reward mirroring still kicks in.
        assert result.reward == 0.5

    def test_full_legacy_construction(self) -> None:
        """All pre-existing fields still accept the same kwargs."""
        result = ScoreResult(
            score=0.8,
            passed=True,
            error=None,
            details={"f1": 0.8},
            reward_score=0.8,
            ir_metrics={"recall": 0.9, "precision": 0.7, "f1": 0.8},
            scorer_family="oracle_overlap_f1",
            sub_scores={"recall": 0.9, "precision": 0.7, "f1": 0.8},
            diagnostics={"runtime_seconds": 12.3},
            reward=0.8,
        )
        assert result.scorer_family == "oracle_overlap_f1"
        assert result.verdict is None
        assert result.materialized_via == "in_place"
