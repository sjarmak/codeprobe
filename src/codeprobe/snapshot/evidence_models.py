"""Immutable models and closed vocabularies for evidence bundles."""

from __future__ import annotations

from dataclasses import dataclass

REQUEST_SCHEMA_VERSION = "codeprobe.zero-code-access.request.v1"
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MIN_PAIRED_TASKS = 10
MIN_REPEATS_PER_TASK = 3

CONFIGURATION_IDS = ("A", "B")
EXECUTION_LOCATIONS = ("data_owner_environment",)
REPOSITORY_ACCESS_LEVELS = ("data_owner_only",)
NETWORK_POSTURES = ("offline", "restricted", "approved")
SELECTION_METHODS = (
    "predeclared_explicit",
    "predeclared_stratified",
    "predeclared_window",
)
EXCLUSION_REASONS = (
    "predeclared",
    "duplicate",
    "out_of_window",
    "unsupported_task_type",
)
QUALITY_METRICS = ("mean_score", "pass_rate")
EFFECT_SIZE_METHODS = ("cliffs_delta", "cohens_d", "mcnemar", "none", "wilcoxon")
REPORT_REFUSAL_CODES = (
    "below_paired_floor",
    "disjoint_task_sets",
    "invalid_run",
    "other_structural_refusal",
)
DECLARED_VALIDITY_WARNING_CODES = (
    "abandoned_tool_surface",
    "cost_not_comparable",
    "infra_failure",
    "partial_run",
    "report_refused",
)
DERIVED_VALIDITY_WARNING_CODES = (
    "below_paired_task_floor",
    "different_task_sets",
    "disqualifying_support",
    "incomplete_repeats",
    "sample_changed_after_results",
    "sample_not_representative",
)
VALIDITY_WARNING_CODES = tuple(
    sorted(frozenset(DECLARED_VALIDITY_WARNING_CODES) | frozenset(DERIVED_VALIDITY_WARNING_CODES))
)
CONCLUSIONS = ("advance_a", "advance_b", "insufficient_evidence")
ACTOR_ROLES = (
    "data_owner_security",
    "data_owner_technical_owner",
    "other_provider_personnel",
    "provider_engineering",
    "provider_support",
)
SUPPORT_KINDS = (
    "asynchronous_coordination",
    "bespoke_code_change",
    "direct_environment_access",
    "generic_guidance",
    "live_collaboration",
    "manual_evidence_repair",
    "prohibited_data_sharing",
    "published_runbook",
    "raw_data_received",
    "raw_result_reinterpretation",
    "sanitized_diagnostics",
    "security_followup",
)

_PROVIDER_ROLES = frozenset(
    {
        "other_provider_personnel",
        "provider_engineering",
        "provider_support",
    }
)
_DISQUALIFYING_KINDS = frozenset(
    {
        "bespoke_code_change",
        "manual_evidence_repair",
        "prohibited_data_sharing",
        "raw_data_received",
        "raw_result_reinterpretation",
    }
)


class EvidenceBundleValidationError(ValueError):
    """Raised when content cannot enter the evidence export boundary."""


@dataclass(frozen=True)
class EnvironmentPosture:
    execution_location: str
    repository_access: str
    network_posture: str


@dataclass(frozen=True)
class ConfigurationIdentity:
    configuration_id: str
    configuration_digest: str


@dataclass(frozen=True)
class RunRequest:
    codeprobe_version: str
    environment: EnvironmentPosture
    configurations: tuple[ConfigurationIdentity, ...]


@dataclass(frozen=True)
class SampleWindow:
    start: str
    end: str


@dataclass(frozen=True)
class TaskPairDigest:
    task_digest: str
    verifier_digest: str
    category_id: str


@dataclass(frozen=True)
class CategoryCount:
    category_id: str
    selected_count: int
    paired_scorable_count: int


@dataclass(frozen=True)
class ExclusionCount:
    reason: str
    count: int


@dataclass(frozen=True)
class SampleRequest:
    window: SampleWindow
    selection_method: str
    changed_after_results: bool
    task_pairs: tuple[TaskPairDigest, ...]
    category_counts: tuple[CategoryCount, ...]
    exclusions: tuple[ExclusionCount, ...]
    attrition_count: int
    representative: bool


@dataclass(frozen=True)
class Interval:
    lower: float
    upper: float


@dataclass(frozen=True)
class ConfigurationAggregate:
    configuration_id: str
    scorable_run_count: int
    total_run_count: int
    mean_quality: float
    quality_ci: Interval
    total_cost_usd: float | None
    cost_coverage: float
    mean_latency_seconds: float
    total_latency_seconds: float


@dataclass(frozen=True)
class ComparisonAggregate:
    report_comparable: bool
    report_refusal_code: str | None
    score_difference: float
    cost_difference_usd: float | None
    latency_difference_seconds: float
    p_value: float | None
    effect_size: float | None
    effect_size_method: str
    confidence_interval: Interval


@dataclass(frozen=True)
class ResultsRequest:
    quality_metric: str
    repeats_per_task: int
    paired_task_count: int
    paired_task_set_same: bool
    configurations: tuple[ConfigurationAggregate, ...]
    comparison: ComparisonAggregate
    validity_warnings: tuple[str, ...]


@dataclass(frozen=True)
class FindingRequest:
    conclusion: str


@dataclass(frozen=True)
class SupportEvent:
    sequence: int
    actor_role: str
    kind: str

    @property
    def disqualifying(self) -> bool:
        """Return whether this event invalidates an external proof."""
        if self.actor_role == "provider_engineering":
            return True
        if (
            self.kind == "direct_environment_access"
            and self.actor_role in _PROVIDER_ROLES
        ):
            return True
        return self.kind in _DISQUALIFYING_KINDS


@dataclass(frozen=True)
class SupportRequest:
    events: tuple[SupportEvent, ...]

    @property
    def disqualified(self) -> bool:
        return any(event.disqualifying for event in self.events)


@dataclass(frozen=True)
class EvidenceRequest:
    schema_version: str
    run: RunRequest
    sample: SampleRequest
    results: ResultsRequest
    finding: FindingRequest
    support: SupportRequest
