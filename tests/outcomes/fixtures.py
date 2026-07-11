"""Deterministic synthetic fixtures for the outcome-linkage prototype.

The linkage set embeds three deliberate traps so that error behaviour is
*measured*, not assumed:

* ``T6`` — a patch-digest collision across two changes (``C6a``/``C6b``): the
  resolver must refuse (ambiguous) rather than pick one, producing a missed link.
* ``T8`` — a patch-digest collision with an *unrelated* change ``C8`` whose true
  origin is not ``T8`` at all: the resolver links confidently and *wrongly*,
  producing a false link that a HIGH confidence floor does NOT remove.
* ``T7`` — a trial with no production change at all: the correct answer is
  "unlinked", so an emitted link would be a false positive.

All fixture records live in a single repository (``repoA``); cross-repo scoping
is exercised separately in the tests, not here, so the measured metrics stay
stable.
"""

from __future__ import annotations

from codeprobe.outcomes import (
    ChangeRecord,
    CohortMember,
    LinkConfidence,
    StratumKey,
    TrialFingerprint,
)

REPO = "repoA"

CHANGES: tuple[ChangeRecord, ...] = (
    ChangeRecord(
        "C1",
        REPO,
        embedded_markers=("M1",),
        patch_digest="D1",
        commit_sha="S1",
        pr_number="P1",
    ),
    ChangeRecord("C2", REPO, patch_digest="D2"),
    ChangeRecord("C3", REPO, commit_sha="S3"),
    ChangeRecord("C4", REPO, pr_number="P4"),
    ChangeRecord("C5", REPO),  # heuristic-only target
    ChangeRecord("C6a", REPO, patch_digest="D6"),
    ChangeRecord("C6b", REPO, patch_digest="D6"),  # collision with C6a
    ChangeRecord(
        "C8", REPO, patch_digest="DX"
    ),  # collides with T8 but is not its change
)

TRIALS: tuple[TrialFingerprint, ...] = (
    TrialFingerprint("T1", REPO, run_marker="M1", patch_digest="D1"),  # run marker wins
    TrialFingerprint("T2", REPO, patch_digest="D2"),
    TrialFingerprint("T3", REPO, commit_sha="S3"),
    TrialFingerprint("T4", REPO, pr_number="P4"),
    TrialFingerprint("T5", REPO, heuristic_change_id="C5"),
    TrialFingerprint("T6", REPO, patch_digest="D6"),  # ambiguous → refused
    TrialFingerprint("T7", REPO),  # no signal, no true change
    TrialFingerprint("T8", REPO, patch_digest="DX"),  # confident but wrong
)

TRIALS_BY_ID: dict[str, TrialFingerprint] = {t.trial_id: t for t in TRIALS}

# Ground truth: trial_id → the change it truly produced (or None).
TRUTH: dict[str, str | None] = {
    "T1": "C1",
    "T2": "C2",
    "T3": "C3",
    "T4": "C4",
    "T5": "C5",
    "T6": "C6a",  # real change exists; resolver must refuse the collision
    "T7": None,
    "T8": None,  # DX digest collision — C8 is not T8's change
}


def cohort_members() -> tuple[CohortMember, ...]:
    """Members for a matched-cohort fixture with one clear imbalanced stratum.

    Stratum ``K1`` is matched (both arms, several members each) with an
    engineered "size" imbalance. ``K2`` has an agent-only member (unmatched).
    ``U8`` is below the HIGH confidence floor (excluded before matching).
    """
    k1 = StratumKey("repoA", "single_file", "single_owner", "low")
    k2 = StratumKey("repoA", "multi_file", "multi_owner", "high")
    return (
        CohortMember("U1", "agent", k1, LinkConfidence.HIGH, {"size": 10.0}),
        CohortMember("U2", "agent", k1, LinkConfidence.HIGH, {"size": 12.0}),
        CohortMember("U3", "agent", k1, LinkConfidence.HIGH, {"size": 14.0}),
        CohortMember("U4", "human", k1, LinkConfidence.HIGH, {"size": 30.0}),
        CohortMember("U5", "human", k1, LinkConfidence.HIGH, {"size": 32.0}),
        CohortMember("U6", "human", k1, LinkConfidence.HIGH, {"size": 34.0}),
        CohortMember("U7", "agent", k2, LinkConfidence.HIGH, {"size": 50.0}),
        CohortMember("U8", "agent", k1, LinkConfidence.LOW, {"size": 11.0}),
    )
