"""Identity precedence + linkage-error measurement (decision codeprobe-tsi9.12).

These tests are the AC1 ("linkage error behaviour is measured") and AC4
("independent review reproduces membership") evidence: the numbers are asserted
exactly, so a reviewer re-running the suite reproduces them.
"""

from __future__ import annotations

import pytest

from codeprobe.outcomes import (
    ChangeRecord,
    IdentitySignal,
    LinkConfidence,
    TrialFingerprint,
    evaluate_by_confidence,
    evaluate_linkage,
    link_trial,
    link_trials,
)

from .fixtures import CHANGES, REPO, TRIALS, TRIALS_BY_ID, TRUTH


def test_run_marker_beats_patch_digest() -> None:
    # T1 carries both a run marker and a matching digest; precedence must pick
    # the run marker (CONFIRMED), not the digest (HIGH).
    link = link_trial(TRIALS_BY_ID["T1"], CHANGES)
    assert link.change_id == "C1"
    assert link.signal is IdentitySignal.RUN_MARKER
    assert link.confidence is LinkConfidence.CONFIRMED


def test_ambiguous_collision_is_refused_not_guessed() -> None:
    # T6's digest matches C6a and C6b — refuse rather than pick one.
    link = link_trial(TRIALS_BY_ID["T6"], CHANGES)
    assert link.change_id is None
    assert link.ambiguous is True
    assert link.confidence is LinkConfidence.NONE


def test_cross_signal_same_tier_collision_is_ambiguous() -> None:
    # patch_digest and commit_sha share the HIGH tier. A digest match to a decoy
    # and a commit match to the real change are TWO distinct changes at the same
    # tier — a genuine collision that must be refused, not resolved to whichever
    # signal is checked first.
    trial = TrialFingerprint(
        "T_conflict", REPO, patch_digest="D_SHARED", commit_sha="S_REAL"
    )
    changes = (
        ChangeRecord("decoy", REPO, patch_digest="D_SHARED"),
        ChangeRecord("real", REPO, commit_sha="S_REAL"),
    )
    link = link_trial(trial, changes)
    assert link.change_id is None
    assert link.ambiguous is True
    assert link.confidence is LinkConfidence.NONE


def test_cross_repo_candidate_is_not_linked() -> None:
    # A PR number restarts at 1 in every repo; a same-number change in a DIFFERENT
    # repo must not be linked (repo scoping prevents the cross-repo false link).
    trial = TrialFingerprint("Tx", "repoA", pr_number="1")
    changes = (ChangeRecord("other-repo-change", "repoB", pr_number="1"),)
    link = link_trial(trial, changes)
    assert link.change_id is None
    assert link.confidence is LinkConfidence.NONE


def test_no_signal_stays_unlinked() -> None:
    link = link_trial(TRIALS_BY_ID["T7"], CHANGES)
    assert link.change_id is None
    assert link.ambiguous is False


def test_confidence_tiers_assigned_per_winning_signal() -> None:
    links = {lk.trial_id: lk for lk in link_trials(TRIALS, CHANGES)}
    assert links["T2"].confidence is LinkConfidence.HIGH  # patch digest
    assert links["T3"].confidence is LinkConfidence.HIGH  # commit sha
    assert links["T4"].confidence is LinkConfidence.MEDIUM  # pr number
    assert links["T5"].confidence is LinkConfidence.LOW  # heuristic


def test_linkage_error_measured_at_default_floor() -> None:
    links = link_trials(TRIALS, CHANGES)
    report = evaluate_linkage(links, TRUTH)
    assert report.predicted_positive == 6
    assert report.true_positive == 5
    assert report.false_link == 1  # T8 digest collision
    assert report.missed_link == 1  # T6 refused
    assert report.ambiguous == 1
    assert report.precision == pytest.approx(5 / 6)
    assert report.recall == pytest.approx(5 / 6)
    assert report.false_link_rate == pytest.approx(1 / 6)
    assert report.missed_link_rate == pytest.approx(1 / 6)
    assert report.f1 == pytest.approx(5 / 6)


def test_high_floor_trades_recall_and_does_not_fix_collisions() -> None:
    # A HIGH confidence floor drops the correct MEDIUM/LOW links (T4, T5) but
    # keeps the HIGH-tier digest-collision false link (T8): the floor lowers
    # recall while the false-link COUNT is unchanged. This is the measured
    # basis for the design's claim that run-marker instrumentation — not a
    # confidence floor — is what drives false links down.
    links = link_trials(TRIALS, CHANGES, min_confidence=LinkConfidence.HIGH)
    report = evaluate_linkage(links, TRUTH)
    assert report.true_positive == 3  # T1, T2, T3
    assert report.false_link == 1  # T8 survives the floor
    assert report.missed_link == 3  # T4, T5 dropped + T6 refused
    assert report.recall == pytest.approx(0.5)
    assert report.missed_link_rate == pytest.approx(0.5)
    assert report.f1 == pytest.approx(0.6)  # p=0.75, r=0.5


def test_confirmed_tier_has_zero_false_links() -> None:
    links = link_trials(TRIALS, CHANGES)
    by_tier = evaluate_by_confidence(links, TRUTH)
    assert by_tier[LinkConfidence.CONFIRMED].false_link == 0
    assert by_tier[LinkConfidence.CONFIRMED].precision == pytest.approx(1.0)
    # The HIGH tier is where the collision false link lands.
    assert by_tier[LinkConfidence.HIGH].false_link == 1


def test_empty_change_set_links_nothing() -> None:
    empty: tuple[ChangeRecord, ...] = ()
    link = link_trial(TrialFingerprint("Tx", REPO, run_marker="Z"), empty)
    assert link.change_id is None
    assert link.confidence is LinkConfidence.NONE
