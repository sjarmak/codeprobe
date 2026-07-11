"""Production-outcome identity resolution (decision codeprobe-tsi9.12).

Links an agent *trial* to a production *change* (a merged commit / PR) using a
fixed precedence of identity signals grouped into confidence *tiers*. The tiers,
the confidence mapping, and the repository-scoping precondition are the *selected
policy* recorded in ``docs/investigations/codeprobe-tsi9.12/design.md`` — this
module is the typed, executable form of that decision, not a heuristic.

Resolution proceeds tier by tier, strongest first. Within a tier every signal is
equal-confidence, so the tier is evaluated *jointly*: if two different changes
match via different signals of the same tier (e.g. one via ``patch_digest`` and
another via ``commit_sha``), that is a genuine collision and the link is refused
as ``ambiguous`` rather than silently resolved to whichever signal the code
happened to check first.

Preconditions the caller (an ingestion connector under epic ``codeprobe-tsi9.5``)
must uphold, stated here because this module cannot enforce them without becoming
a validation boundary it is not yet:

* ``repo`` identifies the repository (and, later, tenant). Identity signals are
  only compared within the same ``repo`` — PR numbers and short hashes are not
  globally unique, so cross-repo comparison would manufacture confident-but-wrong
  links. This is enforced here (matching is repo-gated), not left to the caller.
* ``patch_digest`` must be a full-length collision-resistant digest of the
  normalised diff (SHA-256, no truncation); ``run_marker`` must be unique per
  trial (e.g. a UUID4 embedded in the requested patch). The HIGH/CONFIRMED
  confidence tiers assume this entropy; a truncated hash or low-entropy marker
  would inherit a tier it has not earned.

ZFC note: matching is exact equality on structural identifiers only. There is no
semantic scoring and no tunable threshold — the only ordering is the fixed tier
policy below. The low-confidence proximity heuristic (author/time/path) is NOT
synthesised here; a connector that has already done proximity matching supplies
it pre-labelled, so the fuzzy judgment stays out of this deterministic core.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum


class IdentitySignal(Enum):
    """A single identity signal that can tie a trial to a production change."""

    RUN_MARKER = "run_marker"
    PATCH_DIGEST = "patch_digest"
    COMMIT_SHA = "commit_sha"
    PR_NUMBER = "pr_number"
    HEURISTIC = "heuristic"


class LinkConfidence(Enum):
    """Confidence tier of a resolved link, derived from the winning signal."""

    CONFIRMED = "confirmed"  # explicit run marker embedded in the change
    HIGH = "high"  # content digest or commit SHA equality
    MEDIUM = "medium"  # PR-number equality only
    LOW = "low"  # pre-labelled proximity heuristic
    NONE = "none"  # no admissible signal → unlinked

    def rank(self) -> int:
        """Return an ordinal where a lower value means stronger confidence."""
        return _CONFIDENCE_RANK[self]


# Selected policy: signals grouped into confidence tiers, strongest first. Within
# a tier, signals are equal-confidence and evaluated jointly for ambiguity.
# ``patch_digest`` and ``commit_sha`` deliberately share the HIGH tier — a change
# matched by one and a *different* change matched by the other is a real
# collision, not a precedence tiebreak.
_TIERS: tuple[tuple[LinkConfidence, tuple[IdentitySignal, ...]], ...] = (
    (LinkConfidence.CONFIRMED, (IdentitySignal.RUN_MARKER,)),
    (LinkConfidence.HIGH, (IdentitySignal.PATCH_DIGEST, IdentitySignal.COMMIT_SHA)),
    (LinkConfidence.MEDIUM, (IdentitySignal.PR_NUMBER,)),
    (LinkConfidence.LOW, (IdentitySignal.HEURISTIC,)),
)

# Flat strongest-first ordering, derived from the tiers for callers that want the
# precedence as a single list. Order within a tier is the tier's own order.
IDENTITY_PRECEDENCE: tuple[IdentitySignal, ...] = tuple(
    signal for _confidence, signals in _TIERS for signal in signals
)

_SIGNAL_CONFIDENCE: dict[IdentitySignal, LinkConfidence] = {
    signal: confidence for confidence, signals in _TIERS for signal in signals
}

_CONFIDENCE_RANK: dict[LinkConfidence, int] = {
    LinkConfidence.CONFIRMED: 0,
    LinkConfidence.HIGH: 1,
    LinkConfidence.MEDIUM: 2,
    LinkConfidence.LOW: 3,
    LinkConfidence.NONE: 4,
}


def confidence_of(signal: IdentitySignal) -> LinkConfidence:
    """Return the confidence tier declared for ``signal``."""
    return _SIGNAL_CONFIDENCE[signal]


@dataclass(frozen=True)
class TrialFingerprint:
    """What CodeProbe knows about an agent trial for the purpose of linking.

    ``repo`` scopes every identity comparison (see module docstring). ``run_marker``
    is the explicit unique marker CodeProbe embeds in the requested patch
    (prospective instrumentation). ``patch_digest`` is a full-length digest of the
    produced diff. ``commit_sha`` / ``pr_number`` are populated only when a
    connector already resolved them.
    """

    trial_id: str
    repo: str
    run_marker: str | None = None
    patch_digest: str | None = None
    commit_sha: str | None = None
    pr_number: str | None = None
    # Pre-labelled proximity candidate change_id supplied by a connector that
    # already performed author/time/path heuristics. Kept explicit so the
    # heuristic stays out of this deterministic resolver.
    heuristic_change_id: str | None = None


@dataclass(frozen=True)
class ChangeRecord:
    """A production change observed by an outcome connector.

    ``embedded_markers`` holds only the marker *tokens* extracted from the commit
    message / PR body, never the raw free-text body — connectors must minimise to
    the matched token to avoid ingesting author PII (see design.md §7 and the
    retention policy under ``codeprobe-tsi9.11``).
    """

    change_id: str
    repo: str
    embedded_markers: tuple[str, ...] = ()
    patch_digest: str | None = None
    commit_sha: str | None = None
    pr_number: str | None = None


@dataclass(frozen=True)
class OutcomeLink:
    """Resolved (or refused) link between a trial and a production change.

    ``change_id is None`` means unlinked. ``ambiguous`` is set when two or more
    changes matched within the winning confidence tier: a false link is more
    damaging than a missing one, so ambiguity refuses the link.
    """

    trial_id: str
    change_id: str | None
    signal: IdentitySignal | None
    confidence: LinkConfidence
    ambiguous: bool = False


def _matches(
    trial: TrialFingerprint, change: ChangeRecord, signal: IdentitySignal
) -> bool:
    """True when ``signal`` ties ``trial`` to ``change`` (repo already checked)."""
    if signal is IdentitySignal.RUN_MARKER:
        return bool(trial.run_marker) and trial.run_marker in change.embedded_markers
    if signal is IdentitySignal.PATCH_DIGEST:
        return (
            trial.patch_digest is not None and trial.patch_digest == change.patch_digest
        )
    if signal is IdentitySignal.COMMIT_SHA:
        return trial.commit_sha is not None and trial.commit_sha == change.commit_sha
    if signal is IdentitySignal.PR_NUMBER:
        return trial.pr_number is not None and trial.pr_number == change.pr_number
    if signal is IdentitySignal.HEURISTIC:
        return (
            trial.heuristic_change_id is not None
            and change.change_id == trial.heuristic_change_id
        )
    return False


def _signal_in_tier(
    trial: TrialFingerprint, change: ChangeRecord, signals: tuple[IdentitySignal, ...]
) -> IdentitySignal | None:
    """Return the first signal in ``signals`` matching this pair, else ``None``."""
    for signal in signals:
        if _matches(trial, change, signal):
            return signal
    return None


def link_trial(
    trial: TrialFingerprint,
    changes: Sequence[ChangeRecord],
    *,
    min_confidence: LinkConfidence = LinkConfidence.LOW,
) -> OutcomeLink:
    """Resolve ``trial`` against ``changes`` under the fixed tier policy.

    Walks :data:`_TIERS` strongest-first, comparing only changes in the same
    ``repo``. At the first tier with any match, a *single distinct* matched change
    links; two or more distinct matched changes (via any signals of that tier) are
    reported ``ambiguous`` and refused. Links below ``min_confidence`` are dropped
    to ``NONE`` so callers can require, e.g., HIGH identity for line-level survival
    attribution.
    """
    for confidence, signals in _TIERS:
        winners: dict[str, IdentitySignal] = {}
        for change in changes:
            if change.repo != trial.repo:
                continue
            signal = _signal_in_tier(trial, change, signals)
            if signal is not None:
                winners[change.change_id] = signal
        if not winners:
            continue
        if len(winners) > 1:
            return OutcomeLink(
                trial.trial_id, None, None, LinkConfidence.NONE, ambiguous=True
            )
        if confidence.rank() > min_confidence.rank():
            return OutcomeLink(trial.trial_id, None, None, LinkConfidence.NONE)
        change_id, signal = next(iter(winners.items()))
        return OutcomeLink(trial.trial_id, change_id, signal, confidence)
    return OutcomeLink(trial.trial_id, None, None, LinkConfidence.NONE)


def link_trials(
    trials: Sequence[TrialFingerprint],
    changes: Sequence[ChangeRecord],
    *,
    min_confidence: LinkConfidence = LinkConfidence.LOW,
) -> tuple[OutcomeLink, ...]:
    """Resolve every trial independently; deterministic in input order."""
    return tuple(link_trial(t, changes, min_confidence=min_confidence) for t in trials)
