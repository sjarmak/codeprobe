"""Cohort membership determinism + covariate balance (decision codeprobe-tsi9.12).

AC3 ("cohort rules state variables, exclusions, censoring, confounders") and AC4
("independent review reproduces membership") evidence.
"""

from __future__ import annotations

import math

import pytest

from codeprobe.outcomes import (
    SMD_REPORTING_REFERENCE,
    CohortMember,
    CohortSpec,
    LinkConfidence,
    MatchedStratum,
    OutcomeKind,
    StratumKey,
    admits_outcome,
    build_cohort,
    covariate_balance,
)

from .fixtures import cohort_members


def _spec() -> CohortSpec:
    # Strata come from StratumKey; the spec records confounders + identity floor.
    return CohortSpec(
        name="retrospective-matched",
        confounders=("size",),
        min_link_confidence=LinkConfidence.HIGH,
    )


def test_membership_is_deterministic_and_matched_only() -> None:
    cohort = build_cohort(cohort_members(), _spec())
    # Only K1 is matched (both arms present); K2 is agent-only.
    assert len(cohort.matched) == 1
    assert cohort.matched_unit_ids == ("U1", "U2", "U3", "U4", "U5", "U6")


def test_low_confidence_excluded_before_matching() -> None:
    cohort = build_cohort(cohort_members(), _spec())
    assert cohort.excluded_low_confidence == ("U8",)


def test_unmatched_stratum_members_excluded() -> None:
    cohort = build_cohort(cohort_members(), _spec())
    assert cohort.excluded_unmatched == ("U7",)


def test_repeated_build_is_stable() -> None:
    members = cohort_members()
    a = build_cohort(members, _spec())
    b = build_cohort(members, _spec())
    assert a.matched_unit_ids == b.matched_unit_ids


def test_covariate_balance_flags_engineered_imbalance() -> None:
    cohort = build_cohort(cohort_members(), _spec())
    smd = covariate_balance(cohort, "agent", "human")
    # agent size mean 12 (var 4), human size mean 32 (var 4): SMD = 20 / 2 = 10.
    assert smd["size"] == pytest.approx(10.0)
    assert smd["size"] > SMD_REPORTING_REFERENCE


def test_balance_returns_zero_when_arm_missing() -> None:
    cohort = build_cohort(cohort_members(), _spec())
    # No "reviewer" arm exists; balance against it is empty, not an error.
    assert covariate_balance(cohort, "agent", "reviewer") == {}


def test_zero_variance_differing_means_is_infinite_not_balanced() -> None:
    # Both arms are internally constant but their means differ: SMD is undefined,
    # and must read as inf, not the falsely-balanced 0.0.
    k = StratumKey("repoA", "single_file", "single_owner", "low")
    members = (
        CohortMember("A1", "agent", k, LinkConfidence.HIGH, {"size": 10.0}),
        CohortMember("A2", "agent", k, LinkConfidence.HIGH, {"size": 10.0}),
        CohortMember("H1", "human", k, LinkConfidence.HIGH, {"size": 1000.0}),
        CohortMember("H2", "human", k, LinkConfidence.HIGH, {"size": 1000.0}),
    )
    cohort = build_cohort(members, _spec())
    smd = covariate_balance(cohort, "agent", "human")
    assert smd["size"] == math.inf


def test_confounder_list_scopes_the_balance_check() -> None:
    # An unrelated covariate present on members must NOT be balanced when the
    # spec names only "size" as a confounder.
    k = StratumKey("repoA", "single_file", "single_owner", "low")
    members = (
        CohortMember(
            "A1", "agent", k, LinkConfidence.HIGH, {"size": 10.0, "noise": 1.0}
        ),
        CohortMember(
            "A2", "agent", k, LinkConfidence.HIGH, {"size": 12.0, "noise": 9.0}
        ),
        CohortMember(
            "H1", "human", k, LinkConfidence.HIGH, {"size": 30.0, "noise": 2.0}
        ),
        CohortMember(
            "H2", "human", k, LinkConfidence.HIGH, {"size": 32.0, "noise": 8.0}
        ),
    )
    cohort = build_cohort(members, _spec())  # confounders=("size",)
    smd = covariate_balance(cohort, "agent", "human")
    assert set(smd) == {"size"}


def test_for_outcome_sets_floor_from_outcome_definition() -> None:
    survival = CohortSpec.for_outcome(OutcomeKind.CODE_SURVIVAL, "survival")
    review = CohortSpec.for_outcome(OutcomeKind.REVIEW_BURDEN, "review")
    assert survival.min_link_confidence is LinkConfidence.HIGH
    assert review.min_link_confidence is LinkConfidence.MEDIUM


def test_admits_outcome_enforces_identity_floor() -> None:
    # A HIGH-floor cohort backs a HIGH-floor outcome; a MEDIUM-floor cohort does
    # not (its members' identity is too weak for a survival claim).
    high = CohortSpec("c", min_link_confidence=LinkConfidence.HIGH)
    medium = CohortSpec("c", min_link_confidence=LinkConfidence.MEDIUM)
    assert admits_outcome(high, OutcomeKind.CODE_SURVIVAL) is True
    assert admits_outcome(medium, OutcomeKind.CODE_SURVIVAL) is False
    # A HIGH cohort over-satisfies a MEDIUM outcome, which is allowed.
    assert admits_outcome(high, OutcomeKind.REVIEW_BURDEN) is True


def test_matched_stratum_members_are_immutable() -> None:
    cohort = build_cohort(cohort_members(), _spec())
    stratum = cohort.matched[0]
    assert isinstance(stratum, MatchedStratum)
    with pytest.raises(TypeError):
        stratum.members_by_arm["agent"] = ()  # type: ignore[index]
