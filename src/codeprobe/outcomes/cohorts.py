"""Cohort construction and validity rules (decision codeprobe-tsi9.12).

The selected matching strategy is *stratified-exact on structural keys* with a
*propensity/covariate-balance check* on residual confounders. Strata are formed
from mechanical repository facts (topology, ownership surface, risk tier); a
stratum is admitted only when both arms (agent and human/baseline) are present,
so unmatched units are excluded rather than silently compared.

ZFC note: stratification keys are structural file-system / VCS facts, and the
balance statistic is a standardised mean difference (deterministic arithmetic).
No semantic risk judgment happens here — if a semantic risk tier is ever wanted,
it is produced upstream by a model and passed in as an opaque ``risk_tier``
string. Membership is fully deterministic in input order.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

from .identity import LinkConfidence
from .outcomes import OutcomeKind, outcome_definition

# Conventional reporting reference for standardised mean difference. Documented,
# NOT enforced: :func:`covariate_balance` returns raw SMD values and never
# gates on this — the report decides how to present imbalance.
SMD_REPORTING_REFERENCE: float = 0.1


@dataclass(frozen=True)
class StratumKey:
    """Exact-match key over structural change features.

    Every field is a mechanical bucket: ``change_topology`` from touched-file
    structure ("single_file" / "multi_file" / "cross_module"), ``ownership_surface``
    from CODEOWNERS ("single_owner" / "multi_owner" / "unowned"), ``risk_tier``
    from structural flags (touches tests / migration / config).
    """

    repo: str
    change_topology: str
    ownership_surface: str
    risk_tier: str


@dataclass(frozen=True)
class CohortMember:
    """One unit eligible for cohort matching.

    ``covariates`` is typed read-only (:class:`Mapping`), but ``frozen=True`` only
    blocks attribute rebinding, not in-place mutation of a plain ``dict`` passed
    by the caller. Treat the mapping as immutable; connectors should pass a
    ``MappingProxyType`` when they need the guarantee enforced.
    """

    unit_id: str
    arm: str  # "agent" (or an agent-policy id) vs "human" / baseline
    stratum: StratumKey
    link_confidence: LinkConfidence = LinkConfidence.HIGH
    covariates: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class CohortSpec:
    """Selected validity rules for a matched-cohort study.

    ``confounders`` scopes :func:`covariate_balance` (only the named covariates are
    balanced when this is non-empty). ``exclusions`` and ``censoring`` record the
    policy an ingestion pipeline (epic ``codeprobe-tsi9.5``) applies upstream; they
    are recorded intent, not consumed by this reference module. ``min_link_confidence``
    is the identity floor enforced on members — build it from an outcome's declared
    floor with :meth:`for_outcome` so a cohort cannot be attributed to a claim it
    lacks the identity strength to support.
    """

    name: str
    match_strategy: str = "stratified_exact+propensity"
    exclusions: tuple[str, ...] = ()
    censoring: tuple[str, ...] = ()
    confounders: tuple[str, ...] = ()
    min_link_confidence: LinkConfidence = LinkConfidence.HIGH

    @classmethod
    def for_outcome(
        cls, kind: OutcomeKind, name: str, *, confounders: tuple[str, ...] = ()
    ) -> CohortSpec:
        """Build a spec whose identity floor matches ``kind``'s declared floor.

        This is the wiring the design record calls for: a cohort studying, e.g.,
        ``CODE_SURVIVAL`` (which requires HIGH identity) gets a HIGH floor by
        construction rather than by a caller remembering to set it.
        """
        return cls(
            name=name,
            confounders=confounders,
            min_link_confidence=outcome_definition(kind).min_link_confidence,
        )


@dataclass(frozen=True)
class MatchedStratum:
    """A stratum retained because both arms are present."""

    stratum: StratumKey
    members_by_arm: Mapping[str, tuple[CohortMember, ...]]


@dataclass(frozen=True)
class Cohort:
    """Result of :func:`build_cohort`."""

    spec: CohortSpec
    matched: tuple[MatchedStratum, ...]
    excluded_low_confidence: tuple[str, ...]
    excluded_unmatched: tuple[str, ...]

    @property
    def matched_unit_ids(self) -> tuple[str, ...]:
        """Every retained unit id, deterministic across arms and strata."""
        ids: list[str] = []
        for stratum in self.matched:
            for arm in sorted(stratum.members_by_arm):
                ids.extend(m.unit_id for m in stratum.members_by_arm[arm])
        return tuple(ids)


def build_cohort(
    members: Sequence[CohortMember],
    spec: CohortSpec,
    *,
    required_arms: tuple[str, ...] = ("agent", "human"),
) -> Cohort:
    """Build a stratified-exact cohort deterministically.

    1. Drop members below ``spec.min_link_confidence`` (identity too weak).
    2. Group survivors by exact :class:`StratumKey`.
    3. Retain a stratum only when *every* arm in ``required_arms`` is present;
       members of dropped strata are recorded in ``excluded_unmatched``.
    """
    floor = spec.min_link_confidence.rank()
    kept: list[CohortMember] = []
    excluded_low: list[str] = []
    for m in members:
        if m.link_confidence.rank() > floor:
            excluded_low.append(m.unit_id)
        else:
            kept.append(m)

    by_stratum: dict[StratumKey, dict[str, list[CohortMember]]] = {}
    for m in kept:
        by_stratum.setdefault(m.stratum, {}).setdefault(m.arm, []).append(m)

    matched: list[MatchedStratum] = []
    excluded_unmatched: list[str] = []
    for stratum in sorted(by_stratum, key=_stratum_sort_key):
        arms = by_stratum[stratum]
        if all(arm in arms for arm in required_arms):
            frozen_arms = MappingProxyType(
                {arm: tuple(arms[arm]) for arm in sorted(arms)}
            )
            matched.append(MatchedStratum(stratum, frozen_arms))
        else:
            for arm_members in arms.values():
                excluded_unmatched.extend(m.unit_id for m in arm_members)

    return Cohort(
        spec=spec,
        matched=tuple(matched),
        excluded_low_confidence=tuple(excluded_low),
        excluded_unmatched=tuple(excluded_unmatched),
    )


def _stratum_sort_key(s: StratumKey) -> tuple[str, str, str, str]:
    return (s.repo, s.change_topology, s.ownership_surface, s.risk_tier)


def admits_outcome(spec: CohortSpec, kind: OutcomeKind) -> bool:
    """True when ``spec``'s identity floor is strict enough for ``kind``'s claim.

    An outcome declares the weakest identity confidence it may be claimed on
    (``OutcomeDefinition.min_link_confidence``). A cohort may back that outcome's
    claim only when its own floor is at least that strict — otherwise it admits
    members whose identity is too weak for the claim. Report assembly (epic
    ``codeprobe-tsi9.5``) calls this before attributing a cohort to an outcome.
    """
    return (
        spec.min_link_confidence.rank()
        <= outcome_definition(kind).min_link_confidence.rank()
    )


def covariate_balance(cohort: Cohort, arm_a: str, arm_b: str) -> dict[str, float]:
    """Standardised mean difference per covariate between two arms.

    SMD = (mean_a - mean_b) / pooled_sd. Returns the absolute SMD for every
    covariate observed on both arms, restricted to ``cohort.spec.confounders`` when
    that list is non-empty (so the balance check reflects the recorded intent, not
    whatever covariates happen to be present). A larger value flags residual
    imbalance the propensity step (out of scope here) would adjust; ``inf`` marks
    an undefined SMD (zero within-arm variance but differing means). This function
    only measures, it does not gate.
    """
    a = _pool(cohort, arm_a)
    b = _pool(cohort, arm_b)
    names = set(a) & set(b)
    if cohort.spec.confounders:
        names &= set(cohort.spec.confounders)
    return {name: _smd(a[name], b[name]) for name in sorted(names)}


def _pool(cohort: Cohort, arm: str) -> dict[str, list[float]]:
    """Collect covariate value lists for ``arm`` across all matched strata."""
    pooled: dict[str, list[float]] = {}
    for stratum in cohort.matched:
        for m in stratum.members_by_arm.get(arm, ()):
            for name, value in m.covariates.items():
                pooled.setdefault(name, []).append(float(value))
    return pooled


def _smd(xs: list[float], ys: list[float]) -> float:
    """Absolute standardised mean difference between two samples.

    Returns ``0.0`` when the means coincide, ``inf`` when the pooled standard
    deviation is zero but the means differ (SMD is genuinely undefined there —
    reporting ``0.0`` would falsely read as perfectly balanced), and the finite
    SMD otherwise.
    """
    if not xs or not ys:
        return 0.0
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    if mean_x == mean_y:
        return 0.0
    var_x = _variance(xs, mean_x)
    var_y = _variance(ys, mean_y)
    pooled_sd = math.sqrt((var_x + var_y) / 2)
    if pooled_sd == 0.0:
        return math.inf
    return abs(mean_x - mean_y) / pooled_sd


def _variance(values: list[float], mean: float) -> float:
    if len(values) < 2:
        return 0.0
    return sum((v - mean) ** 2 for v in values) / (len(values) - 1)
