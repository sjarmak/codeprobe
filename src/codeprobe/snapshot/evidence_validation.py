"""Strict validation for the five exported evidence artifacts."""

from __future__ import annotations

import hmac
import re
from collections import Counter
from collections.abc import Mapping
from datetime import date
from typing import Any

from codeprobe import __version__
from codeprobe.snapshot.evidence_approval import approval_digest_for_artifacts
from codeprobe.snapshot.evidence_findings import render_findings
from codeprobe.snapshot.evidence_models import (
    ACTOR_ROLES,
    CONCLUSIONS,
    CONFIGURATION_IDS,
    EFFECT_SIZE_METHODS,
    EXCLUSION_REASONS,
    EXECUTION_LOCATIONS,
    NETWORK_POSTURES,
    QUALITY_METRICS,
    REPORT_REFUSAL_CODES,
    REPOSITORY_ACCESS_LEVELS,
    SELECTION_METHODS,
    SUPPORT_KINDS,
    VALIDITY_WARNING_CODES,
    SupportEvent,
)
from codeprobe.snapshot.evidence_validation_primitives import (
    array_value as _array,
)
from codeprobe.snapshot.evidence_validation_primitives import (
    boolean_value as _boolean,
)
from codeprobe.snapshot.evidence_validation_primitives import (
    digest_value as _digest,
)
from codeprobe.snapshot.evidence_validation_primitives import (
    error as _error,
)
from codeprobe.snapshot.evidence_validation_primitives import (
    integer_value as _integer,
)
from codeprobe.snapshot.evidence_validation_primitives import (
    interval_value as _interval,
)
from codeprobe.snapshot.evidence_validation_primitives import (
    number_value as _number,
)
from codeprobe.snapshot.evidence_validation_primitives import (
    object_array as _object_array,
)
from codeprobe.snapshot.evidence_validation_primitives import (
    object_value as _object,
)
from codeprobe.snapshot.evidence_validation_primitives import (
    optional_number as _optional_number,
)
from codeprobe.snapshot.evidence_validation_primitives import (
    parse_json as _parse_json,
)
from codeprobe.snapshot.evidence_validation_primitives import (
    string_value as _string,
)

RUN_MANIFEST_SCHEMA = "codeprobe.zero-code-access.run-manifest.v1"
SAMPLE_ATTESTATION_SCHEMA = "codeprobe.zero-code-access.sample-attestation.v1"
AGGREGATE_RESULTS_SCHEMA = "codeprobe.zero-code-access.aggregate-results.v1"
SUPPORT_LOG_SCHEMA = "codeprobe.zero-code-access.support-log.v1"

ARTIFACT_FILENAMES = (
    "run-manifest.json",
    "sample-attestation.json",
    "aggregate-results.json",
    "findings.md",
    "support-log.json",
)
APPROVAL_STATEMENTS = ("privacy", "sample_fidelity", "result_fidelity", "usefulness")
_CATEGORY_PATTERN = re.compile(r"category_[0-9]{2,3}")


def _validate_manifest_environment(value: object, path: str) -> None:
    environment = _object(
        value,
        path,
        frozenset({"execution_location", "repository_access", "network_posture"}),
    )
    _string(
        environment["execution_location"],
        f"{path}.execution_location",
        choices=EXECUTION_LOCATIONS,
    )
    _string(
        environment["repository_access"],
        f"{path}.repository_access",
        choices=REPOSITORY_ACCESS_LEVELS,
    )
    _string(
        environment["network_posture"],
        f"{path}.network_posture",
        choices=NETWORK_POSTURES,
    )


def _validate_manifest_configurations(value: object, path: str) -> None:
    configurations = _object_array(
        value,
        path,
        frozenset({"configuration_id", "configuration_digest"}),
        maximum=2,
    )
    for index, configuration in enumerate(configurations):
        _string(
            configuration["configuration_id"],
            f"{path}[{index}].configuration_id",
            choices=CONFIGURATION_IDS,
        )
        _digest(
            configuration["configuration_digest"],
            f"{path}[{index}].configuration_digest",
        )
    if tuple(item["configuration_id"] for item in configurations) != (CONFIGURATION_IDS):
        raise _error(path, "must contain A then B")


def _validate_manifest_counts(value: object, path: str) -> None:
    counts = _object_array(
        value,
        path,
        frozenset({"configuration_id", "scorable_run_count", "total_run_count"}),
        maximum=2,
    )
    for index, count in enumerate(counts):
        _string(
            count["configuration_id"],
            f"{path}[{index}].configuration_id",
            choices=CONFIGURATION_IDS,
        )
        scorable = _integer(
            count["scorable_run_count"],
            f"{path}[{index}].scorable_run_count",
        )
        total = _integer(
            count["total_run_count"],
            f"{path}[{index}].total_run_count",
        )
        if total < scorable:
            raise _error(
                f"{path}[{index}]",
                "total_run_count must cover scorable_run_count",
            )
    if tuple(item["configuration_id"] for item in counts) != CONFIGURATION_IDS:
        raise _error(path, "must contain A then B")


def _validate_manifest(value: Mapping[str, Any]) -> None:
    path = "run-manifest.json"
    manifest = _object(
        value,
        path,
        frozenset(
            {
                "schema_version",
                "approval_digest",
                "artifact_names",
                "codeprobe_version",
                "environment",
                "configurations",
                "run_counts",
            }
        ),
    )
    if manifest["schema_version"] != RUN_MANIFEST_SCHEMA:
        raise _error(f"{path}.schema_version", "unsupported version")
    _digest(manifest["approval_digest"], f"{path}.approval_digest")
    if tuple(_array(manifest["artifact_names"], f"{path}.artifact_names")) != (ARTIFACT_FILENAMES):
        raise _error(f"{path}.artifact_names", "must name the fixed bundle")
    _string(
        manifest["codeprobe_version"],
        f"{path}.codeprobe_version",
        choices=(__version__,),
    )
    _validate_manifest_environment(manifest["environment"], f"{path}.environment")
    _validate_manifest_configurations(
        manifest["configurations"], f"{path}.configurations"
    )
    _validate_manifest_counts(manifest["run_counts"], f"{path}.run_counts")


def _validate_sample_window(value: object, path: str) -> None:
    window = _object(value, path, frozenset({"start", "end"}))
    try:
        start = date.fromisoformat(_string(window["start"], f"{path}.start"))
        end = date.fromisoformat(_string(window["end"], f"{path}.end"))
    except ValueError:
        raise _error(path, "dates must use YYYY-MM-DD") from None
    if end < start:
        raise _error(path, "end must not precede start")


def _validate_task_pairs(
    value: object, path: str
) -> tuple[Mapping[str, Any], ...]:
    pairs = _object_array(
        value,
        path,
        frozenset({"task_digest", "verifier_digest", "category_id"}),
    )
    for index, pair in enumerate(pairs):
        _digest(pair["task_digest"], f"{path}[{index}].task_digest")
        _digest(pair["verifier_digest"], f"{path}[{index}].verifier_digest")
        _string(
            pair["category_id"],
            f"{path}[{index}].category_id",
            pattern=_CATEGORY_PATTERN,
        )
    digests = tuple(item["task_digest"] for item in pairs)
    if len(frozenset(digests)) != len(digests):
        raise _error(path, "task_digest values must be unique")
    return pairs


def _validate_category_item(
    category: Mapping[str, Any], index: int, path: str
) -> None:
    item_path = f"{path}[{index}]"
    _string(
        category["category_id"],
        f"{item_path}.category_id",
        pattern=_CATEGORY_PATTERN,
    )
    selected = _integer(
        category["selected_count"], f"{item_path}.selected_count"
    )
    paired = _integer(
        category["paired_scorable_count"],
        f"{item_path}.paired_scorable_count",
    )
    if paired > selected:
        raise _error(
            item_path,
            "paired_scorable_count must not exceed selected_count",
        )


def _validate_categories(
    value: object,
    task_pairs: tuple[Mapping[str, Any], ...],
    path: str,
) -> tuple[Mapping[str, Any], ...]:
    categories = _object_array(
        value,
        path,
        frozenset({"category_id", "selected_count", "paired_scorable_count"}),
        maximum=100,
    )
    for index, category in enumerate(categories):
        _validate_category_item(category, index, path)
    category_ids = tuple(item["category_id"] for item in categories)
    if len(frozenset(category_ids)) != len(category_ids):
        raise _error(path, "category_id must be unique")
    if frozenset(category_ids) != frozenset(
        item["category_id"] for item in task_pairs
    ):
        raise _error(path, "must cover exactly the anonymous task categories")
    declared = {
        item["category_id"]: item["selected_count"] for item in categories
    }
    actual = Counter(item["category_id"] for item in task_pairs)
    if declared != actual:
        raise _error(
            path,
            "selected_count must match task_pairs for every category",
        )
    return categories


def _validate_exclusions(value: object, path: str) -> None:
    exclusions = _object_array(
        value, path, frozenset({"reason", "count"}), maximum=20
    )
    for index, exclusion in enumerate(exclusions):
        _string(
            exclusion["reason"],
            f"{path}[{index}].reason",
            choices=EXCLUSION_REASONS,
        )
        _integer(exclusion["count"], f"{path}[{index}].count")


def _validate_sample_totals(
    sample: Mapping[str, Any],
    categories: tuple[Mapping[str, Any], ...],
    task_count: int,
    path: str,
) -> None:
    attrition = _integer(sample["attrition_count"], f"{path}.attrition_count")
    _boolean(sample["representative"], f"{path}.representative")
    selected = sum(int(item["selected_count"]) for item in categories)
    paired = sum(int(item["paired_scorable_count"]) for item in categories)
    if selected != task_count:
        raise _error(
            f"{path}.category_counts",
            "selected_count total must equal task_pairs length",
        )
    if attrition != selected - paired:
        raise _error(
            f"{path}.attrition_count",
            "must equal selected minus paired scorable tasks",
        )


def _validate_owner_attestation(value: object, path: str) -> None:
    attestation = _object(
        value,
        path,
        frozenset({"approval_method", "approval_digest", "statements"}),
    )
    if attestation["approval_method"] != "data_owner_supplied_bound_digest":
        raise _error(
            f"{path}.approval_method",
            "contains an unsupported value",
        )
    _digest(attestation["approval_digest"], f"{path}.approval_digest")
    statements = tuple(
        _array(
            attestation["statements"],
            f"{path}.statements",
            maximum=len(APPROVAL_STATEMENTS),
        )
    )
    if statements != APPROVAL_STATEMENTS:
        raise _error(
            f"{path}.statements",
            "must contain the four fixed approval statements",
        )


def _validate_sample(value: Mapping[str, Any]) -> None:
    path = "sample-attestation.json"
    sample = _object(
        value,
        path,
        frozenset(
            {
                "schema_version",
                "approval_digest",
                "window",
                "selection_method",
                "changed_after_results",
                "task_pairs",
                "category_counts",
                "exclusions",
                "attrition_count",
                "representative",
                "data_owner_attestation",
            }
        ),
    )
    if sample["schema_version"] != SAMPLE_ATTESTATION_SCHEMA:
        raise _error(f"{path}.schema_version", "unsupported version")
    _digest(sample["approval_digest"], f"{path}.approval_digest")
    _validate_sample_window(sample["window"], f"{path}.window")
    _string(
        sample["selection_method"],
        f"{path}.selection_method",
        choices=SELECTION_METHODS,
    )
    _boolean(sample["changed_after_results"], f"{path}.changed_after_results")
    task_pairs = _validate_task_pairs(
        sample["task_pairs"], f"{path}.task_pairs"
    )
    categories = _validate_categories(
        sample["category_counts"], task_pairs, f"{path}.category_counts"
    )
    _validate_exclusions(sample["exclusions"], f"{path}.exclusions")
    _validate_sample_totals(sample, categories, len(task_pairs), path)
    _validate_owner_attestation(
        sample["data_owner_attestation"],
        f"{path}.data_owner_attestation",
    )


def _validate_configuration_aggregate(
    configuration: Mapping[str, Any], item_path: str
) -> None:
    _string(
        configuration["configuration_id"],
        f"{item_path}.configuration_id",
        choices=CONFIGURATION_IDS,
    )
    scorable = _integer(
        configuration["scorable_run_count"],
        f"{item_path}.scorable_run_count",
    )
    total = _integer(
        configuration["total_run_count"], f"{item_path}.total_run_count"
    )
    if total < scorable:
        raise _error(item_path, "total_run_count must cover scorable runs")
    _number(
        configuration["mean_quality"],
        f"{item_path}.mean_quality",
        minimum=0.0,
        maximum=1.0,
    )
    _interval(
        configuration["quality_ci"],
        f"{item_path}.quality_ci",
        bounded_quality=True,
    )
    cost = _optional_number(
        configuration["total_cost_usd"],
        f"{item_path}.total_cost_usd",
        minimum=0.0,
    )
    coverage = _number(
        configuration["cost_coverage"],
        f"{item_path}.cost_coverage",
        minimum=0.0,
        maximum=1.0,
    )
    if (cost is None) != (coverage == 0.0):
        raise _error(
            item_path,
            "total_cost_usd must be null exactly when coverage is zero",
        )
    for field in ("mean_latency_seconds", "total_latency_seconds"):
        _number(configuration[field], f"{item_path}.{field}", minimum=0.0)


def _validate_configuration_aggregates(
    value: object,
) -> tuple[Mapping[str, Any], ...]:
    path = "aggregate-results.json.configurations"
    configurations = _object_array(
        value,
        path,
        frozenset(
            {
                "configuration_id",
                "scorable_run_count",
                "total_run_count",
                "mean_quality",
                "quality_ci",
                "total_cost_usd",
                "cost_coverage",
                "mean_latency_seconds",
                "total_latency_seconds",
            }
        ),
        maximum=2,
    )
    for index, configuration in enumerate(configurations):
        _validate_configuration_aggregate(configuration, f"{path}[{index}]")
    if tuple(item["configuration_id"] for item in configurations) != (CONFIGURATION_IDS):
        raise _error(path, "must contain A then B")
    return configurations


def _validate_report_status(
    comparison: Mapping[str, Any], path: str
) -> None:
    comparable = _boolean(
        comparison["report_comparable"], f"{path}.report_comparable"
    )
    refusal = comparison["report_refusal_code"]
    if refusal is not None:
        _string(
            refusal,
            f"{path}.report_refusal_code",
            choices=REPORT_REFUSAL_CODES,
        )
    if comparable == (refusal is not None):
        raise _error(
            path,
            "report_refusal_code must be null exactly when comparable",
        )


def _validate_comparison(value: object) -> None:
    path = "aggregate-results.json.comparison"
    comparison = _object(
        value,
        path,
        frozenset(
            {
                "report_comparable",
                "report_refusal_code",
                "score_difference",
                "cost_difference_usd",
                "latency_difference_seconds",
                "p_value",
                "effect_size",
                "effect_size_method",
                "confidence_interval",
            }
        ),
    )
    _validate_report_status(comparison, path)
    _number(comparison["score_difference"], f"{path}.score_difference")
    _optional_number(comparison["cost_difference_usd"], f"{path}.cost_difference_usd")
    _number(
        comparison["latency_difference_seconds"],
        f"{path}.latency_difference_seconds",
    )
    p_value = _optional_number(comparison["p_value"], f"{path}.p_value")
    if p_value is not None and not 0.0 <= p_value <= 1.0:
        raise _error(f"{path}.p_value", "must be between 0 and 1")
    _optional_number(comparison["effect_size"], f"{path}.effect_size")
    _string(
        comparison["effect_size_method"],
        f"{path}.effect_size_method",
        choices=EFFECT_SIZE_METHODS,
    )
    _interval(comparison["confidence_interval"], f"{path}.confidence_interval")


def _validate_warnings(value: object, path: str) -> tuple[str, ...]:
    warnings = tuple(
        _string(
            item,
            f"{path}[{index}]",
            choices=VALIDITY_WARNING_CODES,
        )
        for index, item in enumerate(
            _array(value, path, maximum=len(VALIDITY_WARNING_CODES))
        )
    )
    if warnings != tuple(sorted(frozenset(warnings))):
        raise _error(path, "must contain unique codes in sorted order")
    return warnings


def _validate_evidence_sufficiency(
    conclusion: str,
    sufficient: bool,
    warnings: tuple[str, ...],
    path: str,
) -> None:
    if sufficient != (not warnings):
        raise _error(
            f"{path}.evidence_sufficient",
            "must be false exactly when validity warnings exist",
        )
    if conclusion != "insufficient_evidence" and not sufficient:
        raise _error(
            f"{path}.conclusion",
            "advance conclusion requires sufficient evidence",
        )


def _validate_aggregate(value: Mapping[str, Any]) -> None:
    path = "aggregate-results.json"
    aggregate = _object(
        value,
        path,
        frozenset(
            {
                "schema_version",
                "approval_digest",
                "conclusion",
                "evidence_sufficient",
                "quality_metric",
                "repeats_per_task",
                "paired_task_count",
                "paired_task_set_same",
                "configurations",
                "comparison",
                "validity_warnings",
            }
        ),
    )
    if aggregate["schema_version"] != AGGREGATE_RESULTS_SCHEMA:
        raise _error(f"{path}.schema_version", "unsupported version")
    _digest(aggregate["approval_digest"], f"{path}.approval_digest")
    conclusion = _string(aggregate["conclusion"], f"{path}.conclusion", choices=CONCLUSIONS)
    sufficient = _boolean(aggregate["evidence_sufficient"], f"{path}.evidence_sufficient")
    _string(
        aggregate["quality_metric"],
        f"{path}.quality_metric",
        choices=QUALITY_METRICS,
    )
    _integer(
        aggregate["repeats_per_task"],
        f"{path}.repeats_per_task",
        minimum=1,
    )
    _integer(aggregate["paired_task_count"], f"{path}.paired_task_count")
    _boolean(aggregate["paired_task_set_same"], f"{path}.paired_task_set_same")
    _validate_configuration_aggregates(aggregate["configurations"])
    _validate_comparison(aggregate["comparison"])
    warnings = _validate_warnings(
        aggregate["validity_warnings"], f"{path}.validity_warnings"
    )
    _validate_evidence_sufficiency(
        conclusion, sufficient, warnings, path
    )


def _validate_support(value: Mapping[str, Any]) -> None:
    path = "support-log.json"
    support = _object(
        value,
        path,
        frozenset({"schema_version", "approval_digest", "disqualified", "events"}),
    )
    if support["schema_version"] != SUPPORT_LOG_SCHEMA:
        raise _error(f"{path}.schema_version", "unsupported version")
    _digest(support["approval_digest"], f"{path}.approval_digest")
    disqualified = _boolean(support["disqualified"], f"{path}.disqualified")
    events = _object_array(
        support["events"],
        f"{path}.events",
        frozenset({"sequence", "actor_role", "kind"}),
        maximum=1_000,
    )
    parsed_events = tuple(
        SupportEvent(
            sequence=_integer(event["sequence"], f"{path}.events[{index}].sequence", minimum=1),
            actor_role=_string(
                event["actor_role"],
                f"{path}.events[{index}].actor_role",
                choices=ACTOR_ROLES,
            ),
            kind=_string(
                event["kind"],
                f"{path}.events[{index}].kind",
                choices=SUPPORT_KINDS,
            ),
        )
        for index, event in enumerate(events)
    )
    if tuple(item.sequence for item in parsed_events) != tuple(range(1, len(parsed_events) + 1)):
        raise _error(f"{path}.events", "sequence must be contiguous from 1")
    if disqualified != any(item.disqualifying for item in parsed_events):
        raise _error(
            f"{path}.disqualified",
            "must match the structural support policy",
        )


def _validate_approval_bindings(
    run: Mapping[str, Any],
    sample: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    support: Mapping[str, Any],
) -> None:
    attestation = sample["data_owner_attestation"]
    digests = (
        run["approval_digest"],
        sample["approval_digest"],
        aggregate["approval_digest"],
        support["approval_digest"],
        attestation["approval_digest"],
    )
    if len(frozenset(digests)) != 1:
        raise _error(
            "bundle.approval_digest", "must match across every artifact"
        )
    expected = approval_digest_for_artifacts(run, sample, aggregate, support)
    if not hmac.compare_digest(str(digests[0]), expected):
        raise _error(
            "bundle.approval_digest",
            "must be bound to the exact artifact content",
        )


def _validate_cross_artifact_counts(
    run: Mapping[str, Any],
    sample: Mapping[str, Any],
    aggregate: Mapping[str, Any],
    support: Mapping[str, Any],
) -> None:
    if len(sample["task_pairs"]) - sample["attrition_count"] != aggregate[
        "paired_task_count"
    ]:
        raise _error(
            "bundle.paired_task_count",
            "sample counts must match aggregate results",
        )
    disqualified = "disqualifying_support" in aggregate["validity_warnings"]
    if support["disqualified"] != disqualified:
        raise _error(
            "bundle.disqualifying_support",
            "support status must match validity warnings",
        )
    fields = (
        "configuration_id",
        "scorable_run_count",
        "total_run_count",
    )
    aggregate_counts = tuple(
        tuple(item[field] for field in fields)
        for item in aggregate["configurations"]
    )
    manifest_counts = tuple(
        tuple(item[field] for field in fields) for item in run["run_counts"]
    )
    if manifest_counts != aggregate_counts:
        raise _error(
            "bundle.run_counts",
            "manifest counts must match aggregate results",
        )


def validate_evidence_bundle_documents(
    documents: Mapping[str, str],
) -> None:
    """Validate exact names, schemas, values, and cross-artifact bindings."""
    if frozenset(documents) != frozenset(ARTIFACT_FILENAMES):
        raise _error("bundle", "must contain exactly the five allowlisted artifacts")
    if any(not isinstance(documents[name], str) for name in ARTIFACT_FILENAMES):
        raise _error("bundle", "every artifact must be text")
    run = _parse_json(documents, "run-manifest.json")
    sample = _parse_json(documents, "sample-attestation.json")
    aggregate = _parse_json(documents, "aggregate-results.json")
    support = _parse_json(documents, "support-log.json")
    _validate_manifest(run)
    _validate_sample(sample)
    _validate_aggregate(aggregate)
    _validate_support(support)
    _validate_cross_artifact_counts(run, sample, aggregate, support)
    if documents["findings.md"] != render_findings(aggregate):
        raise _error("findings.md", "must match the fixed aggregate findings schema")
    _validate_approval_bindings(run, sample, aggregate, support)
