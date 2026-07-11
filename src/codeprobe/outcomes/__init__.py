"""Production outcome identity + cohort contract (decision codeprobe-tsi9.12).

This package is the executable form of the decision recorded in
``docs/investigations/codeprobe-tsi9.12/design.md``: how an agent trial is linked
to a production change (with declared identity confidence), what post-merge
outcomes mean, and the validity rules for matched-cohort studies. Epic
``codeprobe-tsi9.5`` implements the ingestion pipeline against these types; this
package deliberately contains no connectors, storage, or model fitting.
"""

from __future__ import annotations

from .cohorts import (
    SMD_REPORTING_REFERENCE,
    Cohort,
    CohortMember,
    CohortSpec,
    MatchedStratum,
    StratumKey,
    admits_outcome,
    build_cohort,
    covariate_balance,
)
from .identity import (
    IDENTITY_PRECEDENCE,
    ChangeRecord,
    IdentitySignal,
    LinkConfidence,
    OutcomeLink,
    TrialFingerprint,
    confidence_of,
    link_trial,
    link_trials,
)
from .linkage_metrics import (
    LinkageReport,
    evaluate_by_confidence,
    evaluate_linkage,
)
from .outcomes import (
    OUTCOME_DEFINITIONS,
    CensoringRule,
    ClaimClass,
    OutcomeDefinition,
    OutcomeKind,
    WindowType,
    outcome_definition,
)

__all__ = [
    "IDENTITY_PRECEDENCE",
    "OUTCOME_DEFINITIONS",
    "SMD_REPORTING_REFERENCE",
    "CensoringRule",
    "ChangeRecord",
    "ClaimClass",
    "Cohort",
    "CohortMember",
    "CohortSpec",
    "IdentitySignal",
    "LinkConfidence",
    "LinkageReport",
    "MatchedStratum",
    "OutcomeDefinition",
    "OutcomeKind",
    "OutcomeLink",
    "StratumKey",
    "TrialFingerprint",
    "WindowType",
    "admits_outcome",
    "build_cohort",
    "confidence_of",
    "covariate_balance",
    "evaluate_by_confidence",
    "evaluate_linkage",
    "link_trial",
    "link_trials",
    "outcome_definition",
]
