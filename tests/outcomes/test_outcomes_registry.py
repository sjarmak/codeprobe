"""Outcome-taxonomy registry completeness (decision codeprobe-tsi9.12).

AC2 evidence: every outcome has an operational definition and an observation
window, plus the selected censoring/competing-risk/identity-floor/claim-class
fields. Also guards the causal-discipline invariant: no outcome may be claimed
as causal (the enum only offers descriptive/association).
"""

from __future__ import annotations

from codeprobe.outcomes import (
    OUTCOME_DEFINITIONS,
    CensoringRule,
    ClaimClass,
    LinkConfidence,
    OutcomeKind,
    WindowType,
    outcome_definition,
)


def test_all_six_outcomes_defined() -> None:
    assert set(OUTCOME_DEFINITIONS) == set(OutcomeKind)
    assert len(OUTCOME_DEFINITIONS) == 6


def test_every_definition_is_complete() -> None:
    for kind in OutcomeKind:
        d = outcome_definition(kind)
        assert d.operational_definition.strip()
        assert d.counterexample.strip()
        assert d.censoring, f"{kind} declares no censoring rule"
        assert isinstance(d.window_type, WindowType)
        assert isinstance(d.min_link_confidence, LinkConfidence)
        # Every window is either fixed/hybrid with checkpoints, or a pure event
        # window with none — never fixed/hybrid with an empty checkpoint set.
        if d.window_type in (WindowType.FIXED, WindowType.HYBRID):
            assert d.window_days, f"{kind} has no checkpoints for a {d.window_type}"


def test_no_outcome_is_claimed_causal() -> None:
    for kind in OutcomeKind:
        assert outcome_definition(kind).claim_class in (
            ClaimClass.DESCRIPTIVE,
            ClaimClass.ASSOCIATION,
        )


def test_survival_requires_high_identity_review_burden_medium() -> None:
    # Line-level survival attribution needs digest/commit identity (HIGH);
    # PR-level review burden can run on MEDIUM (PR-number) identity.
    assert (
        outcome_definition(OutcomeKind.CODE_SURVIVAL).min_link_confidence
        is LinkConfidence.HIGH
    )
    assert (
        outcome_definition(OutcomeKind.REVIEW_BURDEN).min_link_confidence
        is LinkConfidence.MEDIUM
    )


def test_event_windows_may_carry_or_omit_checkpoints() -> None:
    # REVERT is an EVENT window that still reports at 30/60/90; REVIEW_BURDEN and
    # HUMAN_TAKEOVER are EVENT windows bounded purely by merge/close with no
    # checkpoints. Both shapes are valid — guard the distinction explicitly.
    assert outcome_definition(OutcomeKind.REVERT).window_type is WindowType.EVENT
    assert outcome_definition(OutcomeKind.REVERT).window_days == (30, 60, 90)
    for kind in (OutcomeKind.REVIEW_BURDEN, OutcomeKind.HUMAN_TAKEOVER):
        d = outcome_definition(kind)
        assert d.window_type is WindowType.EVENT
        assert d.window_days == ()


def test_incident_uses_fixed_ninety_day_window() -> None:
    # Incidents lag; a short window under-counts, so the selection is fixed 90d.
    d = outcome_definition(OutcomeKind.INCIDENT)
    assert d.window_type is WindowType.FIXED
    assert d.window_days == (90,)


def test_competing_risks_reference_real_outcomes() -> None:
    for kind in OutcomeKind:
        for risk in outcome_definition(kind).competing_risks:
            assert risk in OUTCOME_DEFINITIONS
            assert risk is not kind  # nothing competes with itself


def test_administrative_censoring_present_where_windows_are_long() -> None:
    # Survival and incident run to 90d and must admit administrative censoring.
    for kind in (OutcomeKind.CODE_SURVIVAL, OutcomeKind.INCIDENT):
        assert CensoringRule.ADMINISTRATIVE in outcome_definition(kind).censoring
