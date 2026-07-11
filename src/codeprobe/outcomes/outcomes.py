"""Production outcome taxonomy (decision codeprobe-tsi9.12).

Each outcome carries a single operational definition, an observation window, its
censoring rules, its competing risks, the identity-confidence floor required to
admit a trial into it, and the *allowed claim language* given the observational
design. These definitions are the selected policy; the registry
:data:`OUTCOME_DEFINITIONS` is the executable form of the decision record.

Nothing here fits a model or scores a trial — that is epic ``codeprobe-tsi9.5``.
This module only declares what each outcome *means* and under what discipline it
may be claimed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from .identity import LinkConfidence


class OutcomeKind(Enum):
    """The six post-merge outcomes CodeProbe commits to observing."""

    CODE_SURVIVAL = "code_survival"
    REVERT = "revert"
    CORRECTIVE_CHURN = "corrective_churn"
    REVIEW_BURDEN = "review_burden"
    INCIDENT = "incident"
    HUMAN_TAKEOVER = "human_takeover"


class WindowType(Enum):
    """How an outcome's observation window is bounded."""

    FIXED = "fixed"  # closed at fixed day offsets from merge
    EVENT = "event"  # closed at the first triggering event
    HYBRID = "hybrid"  # event-primary with fixed reporting checkpoints


class CensoringRule(Enum):
    """Why an observation may end before the outcome is seen."""

    RIGHT = "right"  # not yet observed when the window closes
    ADMINISTRATIVE = "administrative"  # study cutoff reached
    COMPETING = "competing"  # a competing event terminated observation


class ClaimClass(Enum):
    """Discipline on how an outcome may be talked about.

    The whole bridge is observational, so nothing here licenses a causal claim.
    """

    DESCRIPTIVE = "descriptive"  # a rate/count, no comparison implied
    ASSOCIATION = "association"  # matched-cohort association, not causation


@dataclass(frozen=True)
class OutcomeDefinition:
    """Immutable specification of one production outcome."""

    kind: OutcomeKind
    operational_definition: str
    window_type: WindowType
    # Reporting checkpoints. FIXED/HYBRID windows always carry checkpoints; an
    # EVENT window may carry them too (e.g. REVERT reports revert *rates* at
    # 30/60/90) or leave them empty when it is bounded purely by its event
    # (e.g. REVIEW_BURDEN, HUMAN_TAKEOVER bounded by merge/close).
    window_days: tuple[int, ...]
    censoring: tuple[CensoringRule, ...]
    competing_risks: tuple[OutcomeKind, ...]
    min_link_confidence: LinkConfidence
    claim_class: ClaimClass
    counterexample: str  # a case the definition deliberately excludes


_DEFINITIONS: tuple[OutcomeDefinition, ...] = (
    OutcomeDefinition(
        kind=OutcomeKind.CODE_SURVIVAL,
        operational_definition=(
            "Fraction of the agent-introduced lines still attributable to the "
            "change (git blame) at each checkpoint, over the files it touched."
        ),
        window_type=WindowType.HYBRID,
        window_days=(30, 60, 90),
        censoring=(CensoringRule.RIGHT, CensoringRule.ADMINISTRATIVE),
        competing_risks=(OutcomeKind.REVERT, OutcomeKind.HUMAN_TAKEOVER),
        min_link_confidence=LinkConfidence.HIGH,  # needs digest/commit to blame lines
        claim_class=ClaimClass.ASSOCIATION,
        counterexample=(
            "A pure-formatting reflow that rewrites every line but preserves "
            "semantics reads as 0% survival though nothing was undone."
        ),
    ),
    OutcomeDefinition(
        kind=OutcomeKind.REVERT,
        operational_definition=(
            "A commit that restores the pre-change state of the touched hunks "
            "(git revert or an equivalent inverse diff) lands within the window."
        ),
        window_type=WindowType.EVENT,
        window_days=(30, 60, 90),
        censoring=(CensoringRule.RIGHT,),
        competing_risks=(),
        min_link_confidence=LinkConfidence.HIGH,
        claim_class=ClaimClass.DESCRIPTIVE,
        counterexample=(
            "A later refactor that happens to delete the same lines for an "
            "unrelated reason is not a revert of this change."
        ),
    ),
    OutcomeDefinition(
        kind=OutcomeKind.CORRECTIVE_CHURN,
        operational_definition=(
            "Lines of subsequent commits editing the same hunks within the "
            "window, normalised by the change's own line count."
        ),
        window_type=WindowType.HYBRID,
        window_days=(30, 60, 90),
        censoring=(CensoringRule.RIGHT, CensoringRule.COMPETING),
        competing_risks=(OutcomeKind.REVERT,),
        min_link_confidence=LinkConfidence.HIGH,
        claim_class=ClaimClass.ASSOCIATION,
        counterexample=(
            "Feature growth that extends the same file without touching the "
            "change's lines is churn on the file, not corrective churn."
        ),
    ),
    OutcomeDefinition(
        kind=OutcomeKind.REVIEW_BURDEN,
        operational_definition=(
            "Review rounds, review comments, and time-to-merge on the change's "
            "pull request."
        ),
        window_type=WindowType.EVENT,
        window_days=(),  # bounded by the merge event
        censoring=(CensoringRule.RIGHT,),  # still open at cutoff
        competing_risks=(OutcomeKind.HUMAN_TAKEOVER,),
        min_link_confidence=LinkConfidence.MEDIUM,  # PR-level identity suffices
        claim_class=ClaimClass.DESCRIPTIVE,
        counterexample=(
            "A PR held open by release-freeze policy inflates time-to-merge "
            "without reflecting review effort."
        ),
    ),
    OutcomeDefinition(
        kind=OutcomeKind.INCIDENT,
        operational_definition=(
            "An incident or postmortem record that cites the change (by SHA/PR) "
            "within the window."
        ),
        window_type=WindowType.FIXED,
        window_days=(90,),  # incidents lag; a short window under-counts
        censoring=(CensoringRule.RIGHT, CensoringRule.ADMINISTRATIVE),
        competing_risks=(OutcomeKind.REVERT,),
        min_link_confidence=LinkConfidence.HIGH,
        claim_class=ClaimClass.ASSOCIATION,
        counterexample=(
            "An incident touching the same service but tracing to a different "
            "change must not be attributed here on proximity alone."
        ),
    ),
    OutcomeDefinition(
        kind=OutcomeKind.HUMAN_TAKEOVER,
        operational_definition=(
            "A human commit replaces or completes the agent's work before merge, "
            "or the PR is closed unmerged and reworked by a human."
        ),
        window_type=WindowType.EVENT,
        window_days=(),
        censoring=(CensoringRule.RIGHT,),
        competing_risks=(OutcomeKind.REVERT,),
        min_link_confidence=LinkConfidence.MEDIUM,
        claim_class=ClaimClass.DESCRIPTIVE,
        counterexample=(
            "A trivial rebase or lint-fix commit by a human is maintenance, not "
            "a takeover of the change's substance."
        ),
    ),
)

# Read-only registry: the contract is corrupted if an importer can rebind or
# delete entries, so the shared mapping is a MappingProxyType over the private
# dict. Callers mutate nothing; they read via indexing or ``outcome_definition``.
OUTCOME_DEFINITIONS: Mapping[OutcomeKind, OutcomeDefinition] = MappingProxyType(
    {d.kind: d for d in _DEFINITIONS}
)


def outcome_definition(kind: OutcomeKind) -> OutcomeDefinition:
    """Return the selected definition for ``kind``."""
    return OUTCOME_DEFINITIONS[kind]
