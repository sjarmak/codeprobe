"""Measured linkage-error behaviour for the identity resolver (codeprobe-tsi9.12).

Acceptance criterion 1 ("linkage error behaviour is measured") requires that the
identity policy be evaluated against a labelled ground-truth set rather than
asserted. This module computes false-link / missed-link / precision / recall over
a set of :class:`OutcomeLink` predictions given the true trial→change mapping,
with a per-confidence-tier breakdown so a report can require a confidence floor.

Pure deterministic arithmetic — no thresholds, no judgment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .identity import LinkConfidence, OutcomeLink


@dataclass(frozen=True)
class LinkageReport:
    """Counts and rates for one linkage evaluation.

    ``predicted_positive`` = links the resolver emitted (change_id not None).
    ``true_positive`` = predicted link whose change_id equals ground truth.
    ``false_link`` = predicted link whose change_id disagrees with ground truth
    (wrong target, or a link where truth is "no change"). ``missed_link`` =
    ground truth has a change but the resolver refused / mislinked.
    """

    total: int
    condition_positive: int  # trials whose ground truth is a real change
    predicted_positive: int
    true_positive: int
    false_link: int
    missed_link: int
    ambiguous: int

    @property
    def precision(self) -> float:
        """TP / predicted_positive; 1.0 when nothing was predicted."""
        return (
            self.true_positive / self.predicted_positive
            if self.predicted_positive
            else 1.0
        )

    @property
    def recall(self) -> float:
        """TP / condition_positive; 1.0 when there is nothing to find."""
        return (
            self.true_positive / self.condition_positive
            if self.condition_positive
            else 1.0
        )

    @property
    def false_link_rate(self) -> float:
        """false_link / predicted_positive; 0.0 when nothing was predicted."""
        return (
            self.false_link / self.predicted_positive
            if self.predicted_positive
            else 0.0
        )

    @property
    def missed_link_rate(self) -> float:
        """missed_link / condition_positive; 0.0 when there is nothing to find."""
        return (
            self.missed_link / self.condition_positive
            if self.condition_positive
            else 0.0
        )

    @property
    def f1(self) -> float:
        """Harmonic mean of precision and recall."""
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0


def evaluate_linkage(
    links: Sequence[OutcomeLink],
    truth: Mapping[str, str | None],
) -> LinkageReport:
    """Score ``links`` against ``truth`` (trial_id → true change_id or None).

    A trial absent from ``truth`` is treated as truth ``None`` (no real change),
    so an emitted link for it counts as a false link.
    """
    condition_positive = sum(1 for v in truth.values() if v is not None)
    predicted = tp = false_link = missed = ambiguous = 0
    for link in links:
        true_change = truth.get(link.trial_id)
        if link.ambiguous:
            ambiguous += 1
        if link.change_id is not None:
            predicted += 1
            if link.change_id == true_change:
                tp += 1
            else:
                false_link += 1
        elif true_change is not None:
            missed += 1
    return LinkageReport(
        total=len(links),
        condition_positive=condition_positive,
        predicted_positive=predicted,
        true_positive=tp,
        false_link=false_link,
        missed_link=missed,
        ambiguous=ambiguous,
    )


def evaluate_by_confidence(
    links: Sequence[OutcomeLink],
    truth: Mapping[str, str | None],
) -> dict[LinkConfidence, LinkageReport]:
    """Return a per-confidence-tier :class:`LinkageReport` breakdown.

    Each tier's report scores only the links resolved at that tier, so a caller
    can see, e.g., that CONFIRMED links carry a zero false-link rate while LOW
    heuristic links do not — the basis for an admission floor per outcome.
    """
    by_tier: dict[LinkConfidence, list[OutcomeLink]] = {}
    for link in links:
        by_tier.setdefault(link.confidence, []).append(link)
    return {
        tier: evaluate_linkage(tuple(tier_links), truth)
        for tier, tier_links in by_tier.items()
    }
