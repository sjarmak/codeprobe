"""Strict input schema for zero-code-access evidence bundles.

The request is local-only input. It deliberately contains no free-form text:
every exported string is an enum, anonymous identifier, version, date, or
SHA-256 digest. Structural validation therefore enforces the data boundary
without guessing whether arbitrary prose is sensitive.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path

from codeprobe import __version__
from codeprobe.snapshot.evidence_models import (
    ACTOR_ROLES,
    CONCLUSIONS,
    CONFIGURATION_IDS,
    DECLARED_VALIDITY_WARNING_CODES,
    EFFECT_SIZE_METHODS,
    EXCLUSION_REASONS,
    EXECUTION_LOCATIONS,
    MAX_REQUEST_BYTES,
    MIN_PAIRED_TASKS,
    MIN_REPEATS_PER_TASK,
    NETWORK_POSTURES,
    QUALITY_METRICS,
    REPORT_REFUSAL_CODES,
    REPOSITORY_ACCESS_LEVELS,
    REQUEST_SCHEMA_VERSION,
    SELECTION_METHODS,
    SUPPORT_KINDS,
    CategoryCount,
    ComparisonAggregate,
    ConfigurationAggregate,
    ConfigurationIdentity,
    EnvironmentPosture,
    EvidenceBundleValidationError,
    EvidenceRequest,
    ExclusionCount,
    FindingRequest,
    Interval,
    ResultsRequest,
    RunRequest,
    SampleRequest,
    SampleWindow,
    SupportEvent,
    SupportRequest,
    TaskPairDigest,
)
from codeprobe.snapshot.safe_io import SymlinkEscapeError, read_regular_file

_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_CATEGORY_PATTERN = re.compile(r"category_[0-9]{2,3}")


class _DuplicateJsonFieldError(ValueError):
    """Raised internally when JSON contains an ambiguous object."""


def _reject_duplicate_fields(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    keys = tuple(key for key, _ in pairs)
    if len(frozenset(keys)) != len(keys):
        raise _DuplicateJsonFieldError
    return dict(pairs)


def _error(path: str, message: str) -> EvidenceBundleValidationError:
    return EvidenceBundleValidationError(f"{path}: {message}")


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _error(path, "must be an object")
    if not all(isinstance(key, str) for key in value):
        raise _error(path, "field names must be strings")
    return value


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], path: str) -> None:
    actual = frozenset(value)
    unexpected = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unexpected:
        raise _error(path, "contains unexpected field(s)")
    if missing:
        raise _error(path, f"missing field(s): {', '.join(missing)}")


def _string(
    value: object,
    path: str,
    *,
    choices: Sequence[str] | None = None,
    pattern: re.Pattern[str] | None = None,
) -> str:
    if not isinstance(value, str):
        raise _error(path, "must be a string")
    if choices is not None and value not in choices:
        raise _error(path, "contains an unsupported value")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise _error(path, "has an invalid format")
    return value


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise _error(path, "must be a boolean")
    return value


def _integer(value: object, path: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(path, "must be an integer")
    if value < minimum:
        raise _error(path, f"must be at least {minimum}")
    return value


def _number(
    value: object,
    path: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _error(path, "must be a number")
    try:
        result = float(value)
    except (OverflowError, ValueError):
        raise _error(path, "must be finite") from None
    if not math.isfinite(result):
        raise _error(path, "must be finite")
    if minimum is not None and result < minimum:
        raise _error(path, f"must be at least {minimum}")
    if maximum is not None and result > maximum:
        raise _error(path, f"must be at most {maximum}")
    return result


def _optional_number(
    value: object,
    path: str,
    *,
    minimum: float | None = None,
) -> float | None:
    return None if value is None else _number(value, path, minimum=minimum)


def _list(value: object, path: str, *, maximum: int = 10_000) -> Sequence[object]:
    if not isinstance(value, list):
        raise _error(path, "must be an array")
    if len(value) > maximum:
        raise _error(path, f"must contain at most {maximum} items")
    return value


def _parse_environment(value: object) -> EnvironmentPosture:
    path = "run.environment"
    raw = _mapping(value, path)
    _exact_keys(
        raw,
        frozenset({"execution_location", "repository_access", "network_posture"}),
        path,
    )
    return EnvironmentPosture(
        execution_location=_string(
            raw["execution_location"],
            f"{path}.execution_location",
            choices=EXECUTION_LOCATIONS,
        ),
        repository_access=_string(
            raw["repository_access"],
            f"{path}.repository_access",
            choices=REPOSITORY_ACCESS_LEVELS,
        ),
        network_posture=_string(
            raw["network_posture"],
            f"{path}.network_posture",
            choices=NETWORK_POSTURES,
        ),
    )


def _parse_identity(value: object, index: int) -> ConfigurationIdentity:
    path = f"run.configurations[{index}]"
    raw = _mapping(value, path)
    _exact_keys(
        raw,
        frozenset({"configuration_id", "configuration_digest"}),
        path,
    )
    return ConfigurationIdentity(
        configuration_id=_string(
            raw["configuration_id"],
            f"{path}.configuration_id",
            choices=CONFIGURATION_IDS,
        ),
        configuration_digest=_string(
            raw["configuration_digest"],
            f"{path}.configuration_digest",
            pattern=_DIGEST_PATTERN,
        ),
    )


def _parse_run(value: object) -> RunRequest:
    raw = _mapping(value, "run")
    _exact_keys(
        raw,
        frozenset({"codeprobe_version", "environment", "configurations"}),
        "run",
    )
    identities = tuple(
        _parse_identity(item, index)
        for index, item in enumerate(_list(raw["configurations"], "run.configurations", maximum=2))
    )
    if tuple(item.configuration_id for item in identities) != CONFIGURATION_IDS:
        raise _error("run.configurations", "must contain A then B exactly once")
    return RunRequest(
        codeprobe_version=_string(
            raw["codeprobe_version"],
            "run.codeprobe_version",
            choices=(__version__,),
        ),
        environment=_parse_environment(raw["environment"]),
        configurations=identities,
    )


def _parse_window(value: object) -> SampleWindow:
    raw = _mapping(value, "sample.window")
    _exact_keys(raw, frozenset({"start", "end"}), "sample.window")
    start = _string(raw["start"], "sample.window.start")
    end = _string(raw["end"], "sample.window.end")
    try:
        start_date = date.fromisoformat(start)
        end_date = date.fromisoformat(end)
    except ValueError:
        raise _error("sample.window", "dates must use YYYY-MM-DD") from None
    if end_date < start_date:
        raise _error("sample.window", "end must not precede start")
    return SampleWindow(start=start, end=end)


def _parse_task_pair(value: object, index: int) -> TaskPairDigest:
    path = f"sample.task_pairs[{index}]"
    raw = _mapping(value, path)
    _exact_keys(
        raw,
        frozenset({"task_digest", "verifier_digest", "category_id"}),
        path,
    )
    return TaskPairDigest(
        task_digest=_string(raw["task_digest"], f"{path}.task_digest", pattern=_DIGEST_PATTERN),
        verifier_digest=_string(
            raw["verifier_digest"],
            f"{path}.verifier_digest",
            pattern=_DIGEST_PATTERN,
        ),
        category_id=_string(
            raw["category_id"],
            f"{path}.category_id",
            pattern=_CATEGORY_PATTERN,
        ),
    )


def _parse_category(value: object, index: int) -> CategoryCount:
    path = f"sample.category_counts[{index}]"
    raw = _mapping(value, path)
    _exact_keys(
        raw,
        frozenset({"category_id", "selected_count", "paired_scorable_count"}),
        path,
    )
    selected = _integer(raw["selected_count"], f"{path}.selected_count")
    paired = _integer(
        raw["paired_scorable_count"],
        f"{path}.paired_scorable_count",
    )
    if paired > selected:
        raise _error(
            path,
            "paired_scorable_count must not exceed selected_count",
        )
    return CategoryCount(
        category_id=_string(
            raw["category_id"],
            f"{path}.category_id",
            pattern=_CATEGORY_PATTERN,
        ),
        selected_count=selected,
        paired_scorable_count=paired,
    )


def _parse_exclusion(value: object, index: int) -> ExclusionCount:
    path = f"sample.exclusions[{index}]"
    raw = _mapping(value, path)
    _exact_keys(raw, frozenset({"reason", "count"}), path)
    return ExclusionCount(
        reason=_string(raw["reason"], f"{path}.reason", choices=EXCLUSION_REASONS),
        count=_integer(raw["count"], f"{path}.count"),
    )


def _validate_sample_counts(sample: SampleRequest) -> None:
    task_categories = frozenset(item.category_id for item in sample.task_pairs)
    count_categories = tuple(item.category_id for item in sample.category_counts)
    if len(frozenset(count_categories)) != len(count_categories):
        raise _error("sample.category_counts", "category_id values must be unique")
    if frozenset(count_categories) != task_categories:
        raise _error(
            "sample.category_counts",
            "must cover exactly the anonymous task categories",
        )
    declared_distribution = {
        item.category_id: item.selected_count
        for item in sample.category_counts
    }
    actual_distribution = Counter(
        item.category_id for item in sample.task_pairs
    )
    if declared_distribution != actual_distribution:
        raise _error(
            "sample.category_counts",
            "selected_count must match task_pairs for every category",
        )
    selected = sum(item.selected_count for item in sample.category_counts)
    paired = sum(item.paired_scorable_count for item in sample.category_counts)
    if selected != len(sample.task_pairs):
        raise _error(
            "sample.category_counts",
            "selected_count total must equal task_pairs length",
        )
    if sample.attrition_count != selected - paired:
        raise _error(
            "sample.attrition_count",
            "must equal selected minus paired scorable tasks",
        )


def _parse_task_pairs(value: object) -> tuple[TaskPairDigest, ...]:
    pairs = tuple(
        _parse_task_pair(item, index)
        for index, item in enumerate(_list(value, "sample.task_pairs"))
    )
    digests = tuple(item.task_digest for item in pairs)
    if len(frozenset(digests)) != len(digests):
        raise _error("sample.task_pairs", "task_digest values must be unique")
    return pairs


def _parse_categories(value: object) -> tuple[CategoryCount, ...]:
    return tuple(
        _parse_category(item, index)
        for index, item in enumerate(
            _list(value, "sample.category_counts", maximum=100)
        )
    )


def _parse_exclusions(value: object) -> tuple[ExclusionCount, ...]:
    return tuple(
        _parse_exclusion(item, index)
        for index, item in enumerate(
            _list(value, "sample.exclusions", maximum=20)
        )
    )


def _parse_sample(value: object) -> SampleRequest:
    raw = _mapping(value, "sample")
    _exact_keys(
        raw,
        frozenset(
            {
                "window",
                "selection_method",
                "changed_after_results",
                "task_pairs",
                "category_counts",
                "exclusions",
                "attrition_count",
                "representative",
            }
        ),
        "sample",
    )
    task_pairs = _parse_task_pairs(raw["task_pairs"])
    sample = SampleRequest(
        window=_parse_window(raw["window"]),
        selection_method=_string(
            raw["selection_method"],
            "sample.selection_method",
            choices=SELECTION_METHODS,
        ),
        changed_after_results=_boolean(raw["changed_after_results"], "sample.changed_after_results"),
        task_pairs=task_pairs,
        category_counts=_parse_categories(raw["category_counts"]),
        exclusions=_parse_exclusions(raw["exclusions"]),
        attrition_count=_integer(raw["attrition_count"], "sample.attrition_count"),
        representative=_boolean(raw["representative"], "sample.representative"),
    )
    _validate_sample_counts(sample)
    return sample


def _parse_interval(
    value: object,
    path: str,
    *,
    bounded_quality: bool = False,
) -> Interval:
    raw = _mapping(value, path)
    _exact_keys(raw, frozenset({"lower", "upper"}), path)
    minimum = 0.0 if bounded_quality else None
    maximum = 1.0 if bounded_quality else None
    lower = _number(raw["lower"], f"{path}.lower", minimum=minimum, maximum=maximum)
    upper = _number(raw["upper"], f"{path}.upper", minimum=minimum, maximum=maximum)
    if upper < lower:
        raise _error(path, "upper must not be below lower")
    return Interval(lower=lower, upper=upper)


def _parse_config_run_counts(
    raw: Mapping[str, object], path: str
) -> tuple[int, int]:
    scorable = _integer(
        raw["scorable_run_count"], f"{path}.scorable_run_count"
    )
    total = _integer(raw["total_run_count"], f"{path}.total_run_count")
    if total < scorable:
        raise _error(path, "total_run_count must cover every scorable run")
    return scorable, total


def _parse_config_cost(
    raw: Mapping[str, object], path: str
) -> tuple[float | None, float]:
    cost = _optional_number(
        raw["total_cost_usd"], f"{path}.total_cost_usd", minimum=0.0
    )
    coverage = _number(
        raw["cost_coverage"],
        f"{path}.cost_coverage",
        minimum=0.0,
        maximum=1.0,
    )
    if (cost is None) != (coverage == 0.0):
        raise _error(
            path,
            "total_cost_usd must be null exactly when cost_coverage is zero",
        )
    return cost, coverage


def _parse_latencies(
    raw: Mapping[str, object], path: str
) -> tuple[float, float]:
    return (
        _number(
            raw["mean_latency_seconds"],
            f"{path}.mean_latency_seconds",
            minimum=0.0,
        ),
        _number(
            raw["total_latency_seconds"],
            f"{path}.total_latency_seconds",
            minimum=0.0,
        ),
    )


def _parse_config_aggregate(value: object, index: int) -> ConfigurationAggregate:
    path = f"results.configurations[{index}]"
    raw = _mapping(value, path)
    _exact_keys(
        raw,
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
        path,
    )
    scorable, total = _parse_config_run_counts(raw, path)
    cost, coverage = _parse_config_cost(raw, path)
    mean_latency, total_latency = _parse_latencies(raw, path)
    return ConfigurationAggregate(
        configuration_id=_string(
            raw["configuration_id"],
            f"{path}.configuration_id",
            choices=CONFIGURATION_IDS,
        ),
        scorable_run_count=scorable,
        total_run_count=total,
        mean_quality=_number(
            raw["mean_quality"],
            f"{path}.mean_quality",
            minimum=0.0,
            maximum=1.0,
        ),
        quality_ci=_parse_interval(raw["quality_ci"], f"{path}.quality_ci", bounded_quality=True),
        total_cost_usd=cost,
        cost_coverage=coverage,
        mean_latency_seconds=mean_latency,
        total_latency_seconds=total_latency,
    )


def _parse_report_status(
    raw: Mapping[str, object], path: str
) -> tuple[bool, str | None]:
    comparable = _boolean(
        raw["report_comparable"], f"{path}.report_comparable"
    )
    refusal = (
        None
        if raw["report_refusal_code"] is None
        else _string(
            raw["report_refusal_code"],
            f"{path}.report_refusal_code",
            choices=REPORT_REFUSAL_CODES,
        )
    )
    if comparable == (refusal is not None):
        raise _error(
            path,
            "report_refusal_code must be null exactly when report is comparable",
        )
    return comparable, refusal


def _parse_comparison(value: object) -> ComparisonAggregate:
    path = "results.comparison"
    raw = _mapping(value, path)
    _exact_keys(
        raw,
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
        path,
    )
    comparable, refusal = _parse_report_status(raw, path)
    p_value = _optional_number(raw["p_value"], f"{path}.p_value")
    if p_value is not None and not 0.0 <= p_value <= 1.0:
        raise _error(f"{path}.p_value", "must be between 0 and 1")
    return ComparisonAggregate(
        report_comparable=comparable,
        report_refusal_code=refusal,
        score_difference=_number(raw["score_difference"], f"{path}.score_difference"),
        cost_difference_usd=_optional_number(raw["cost_difference_usd"], f"{path}.cost_difference_usd"),
        latency_difference_seconds=_number(
            raw["latency_difference_seconds"],
            f"{path}.latency_difference_seconds",
        ),
        p_value=p_value,
        effect_size=_optional_number(raw["effect_size"], f"{path}.effect_size"),
        effect_size_method=_string(
            raw["effect_size_method"],
            f"{path}.effect_size_method",
            choices=EFFECT_SIZE_METHODS,
        ),
        confidence_interval=_parse_interval(raw["confidence_interval"], f"{path}.confidence_interval"),
    )


def _parse_result_configurations(
    value: object,
) -> tuple[ConfigurationAggregate, ...]:
    configurations = tuple(
        _parse_config_aggregate(item, index)
        for index, item in enumerate(
            _list(value, "results.configurations", maximum=2)
        )
    )
    if (
        tuple(item.configuration_id for item in configurations)
        != CONFIGURATION_IDS
    ):
        raise _error(
            "results.configurations", "must contain A then B exactly once"
        )
    return configurations


def _parse_declared_warnings(value: object) -> tuple[str, ...]:
    warnings = tuple(
        _string(
            item,
            f"results.validity_warnings[{index}]",
            choices=DECLARED_VALIDITY_WARNING_CODES,
        )
        for index, item in enumerate(
            _list(
                value,
                "results.validity_warnings",
                maximum=len(DECLARED_VALIDITY_WARNING_CODES),
            )
        )
    )
    if len(frozenset(warnings)) != len(warnings):
        raise _error(
            "results.validity_warnings", "warning codes must be unique"
        )
    return tuple(sorted(warnings))


def _parse_results(value: object) -> ResultsRequest:
    raw = _mapping(value, "results")
    _exact_keys(
        raw,
        frozenset(
            {
                "quality_metric",
                "repeats_per_task",
                "paired_task_count",
                "paired_task_set_same",
                "configurations",
                "comparison",
                "validity_warnings",
            }
        ),
        "results",
    )
    configs = _parse_result_configurations(raw["configurations"])
    return ResultsRequest(
        quality_metric=_string(
            raw["quality_metric"],
            "results.quality_metric",
            choices=QUALITY_METRICS,
        ),
        repeats_per_task=_integer(raw["repeats_per_task"], "results.repeats_per_task", minimum=1),
        paired_task_count=_integer(raw["paired_task_count"], "results.paired_task_count"),
        paired_task_set_same=_boolean(raw["paired_task_set_same"], "results.paired_task_set_same"),
        configurations=configs,
        comparison=_parse_comparison(raw["comparison"]),
        validity_warnings=_parse_declared_warnings(raw["validity_warnings"]),
    )


def _parse_finding(value: object) -> FindingRequest:
    raw = _mapping(value, "finding")
    _exact_keys(raw, frozenset({"conclusion"}), "finding")
    return FindingRequest(
        conclusion=_string(
            raw["conclusion"],
            "finding.conclusion",
            choices=CONCLUSIONS,
        )
    )


def _parse_support_event(value: object, index: int) -> SupportEvent:
    path = f"support.events[{index}]"
    raw = _mapping(value, path)
    _exact_keys(raw, frozenset({"sequence", "actor_role", "kind"}), path)
    return SupportEvent(
        sequence=_integer(raw["sequence"], f"{path}.sequence", minimum=1),
        actor_role=_string(raw["actor_role"], f"{path}.actor_role", choices=ACTOR_ROLES),
        kind=_string(raw["kind"], f"{path}.kind", choices=SUPPORT_KINDS),
    )


def _parse_support(value: object) -> SupportRequest:
    raw = _mapping(value, "support")
    _exact_keys(raw, frozenset({"events"}), "support")
    events = tuple(
        _parse_support_event(item, index)
        for index, item in enumerate(_list(raw["events"], "support.events", maximum=1_000))
    )
    sequences = tuple(item.sequence for item in events)
    if sequences != tuple(range(1, len(events) + 1)):
        raise _error("support.events", "sequence must be contiguous from 1")
    return SupportRequest(events=events)


def _validate_cross_document_counts(request: EvidenceRequest) -> None:
    paired_from_categories = sum(item.paired_scorable_count for item in request.sample.category_counts)
    if request.results.paired_task_count != paired_from_categories:
        raise _error(
            "results.paired_task_count",
            "must equal category paired_scorable_count total",
        )
    expected_runs = request.results.paired_task_count * request.results.repeats_per_task
    if any(item.scorable_run_count != expected_runs for item in request.results.configurations):
        raise _error(
            "results.configurations",
            "scorable_run_count must equal paired tasks times repeats",
        )


def parse_evidence_request(value: object) -> EvidenceRequest:
    """Parse untrusted JSON into one immutable, exact-schema request."""
    raw = _mapping(value, "request")
    _exact_keys(
        raw,
        frozenset({"schema_version", "run", "sample", "results", "finding", "support"}),
        "request",
    )
    request = EvidenceRequest(
        schema_version=_string(
            raw["schema_version"],
            "request.schema_version",
            choices=(REQUEST_SCHEMA_VERSION,),
        ),
        run=_parse_run(raw["run"]),
        sample=_parse_sample(raw["sample"]),
        results=_parse_results(raw["results"]),
        finding=_parse_finding(raw["finding"]),
        support=_parse_support(raw["support"]),
    )
    _validate_cross_document_counts(request)
    return request


def load_evidence_request(path: Path) -> EvidenceRequest:
    """Load a bounded, regular JSON request without leaking its values."""
    request_path = Path(path)
    try:
        body = read_regular_file(
            request_path.parent,
            request_path.name,
            max_bytes=MAX_REQUEST_BYTES,
        )
    except (OSError, SymlinkEscapeError) as exc:
        raise _error("request", "must be a regular non-symlink file") from exc
    try:
        raw = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_fields,
        )
    except _DuplicateJsonFieldError as exc:
        raise _error("request", "contains duplicate field(s)") from exc
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _error("request", "must contain valid UTF-8 JSON") from exc
    return parse_evidence_request(raw)


__all__ = [
    "EvidenceBundleValidationError",
    "EvidenceRequest",
    "MIN_PAIRED_TASKS",
    "MIN_REPEATS_PER_TASK",
    "REQUEST_SCHEMA_VERSION",
    "load_evidence_request",
    "parse_evidence_request",
]
