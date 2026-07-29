"""Mechanical artifact helpers for the enterprise release journey."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, TypeGuard

_CONFIGURATION_IDS: Final[tuple[str, str]] = ("A", "B")
_SYNTHETIC_CATEGORY: Final[str] = "category_01"
_MIN_SECRET_LENGTH: Final[int] = 8


class EnterpriseHarnessError(RuntimeError):
    """Raised when the release journey cannot produce honest evidence."""


def parse_envelope(stdout: str, command: str) -> dict[str, Any]:
    """Return the single successful machine envelope from command stdout."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if len(lines) != 1:
        raise EnterpriseHarnessError(
            f"{command} did not emit one successful JSON envelope"
        )
    try:
        raw = json.loads(lines[0])
    except json.JSONDecodeError as exc:
        raise EnterpriseHarnessError(
            f"{command} did not emit one successful JSON envelope"
        ) from exc
    if (
        not isinstance(raw, dict)
        or raw.get("record_type") != "envelope"
        or raw.get("ok") is not True
        or not isinstance(raw.get("data"), dict)
    ):
        raise EnterpriseHarnessError(
            f"{command} did not emit one successful JSON envelope"
        )
    return raw


def assert_no_secret_values(
    artifact_paths: Iterable[Path],
    captured_output: Iterable[str],
    secret_values: Sequence[str],
) -> int:
    """Fail when any declared secret occurs in captured output or artifacts."""
    values = tuple(secret_values)
    if not values or any(len(value) < _MIN_SECRET_LENGTH for value in values):
        raise EnterpriseHarnessError(
            "secret values must be declared and at least eight characters"
        )
    haystacks = [text.encode() for text in captured_output]
    haystacks.extend(_read_artifact(path) for path in artifact_paths)
    if any(value.encode() in content for value in values for content in haystacks):
        raise EnterpriseHarnessError("secret scan found a value in journey output")
    return len(values)


def validate_image_labels(
    labels: Mapping[str, Any],
    *,
    version: str,
    commit: str,
) -> None:
    """Require both OCI images to identify the wheel's release candidate."""
    if (
        labels.get("org.opencontainers.image.version") != version
        or labels.get("org.opencontainers.image.revision") != commit
    ):
        raise EnterpriseHarnessError(
            "published image does not carry the candidate labels"
        )


def _read_artifact(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise EnterpriseHarnessError("secret scan could not read an artifact") from exc


def build_evidence_request(
    *,
    report: Mapping[str, Any],
    experiment: Mapping[str, Any],
    candidate_version: str,
) -> dict[str, Any]:
    """Project a two-arm synthetic run into the strict evidence request."""
    configs = _configurations(experiment)
    summaries = _summaries(report)
    paired_tasks = _paired_task_ids(report)
    comparison = _comparison(report)
    run_count = len(paired_tasks)
    results = [
        _configuration_result(config_id, summaries[config_id], run_count)
        for config_id in _CONFIGURATION_IDS
    ]
    return {
        "schema_version": "codeprobe.zero-code-access.request.v1",
        "run": {
            "codeprobe_version": candidate_version,
            "environment": {
                "execution_location": "data_owner_environment",
                "repository_access": "data_owner_only",
                "network_posture": "restricted",
            },
            "configurations": configs,
        },
        "sample": _sample(paired_tasks),
        "results": {
            "quality_metric": "mean_score",
            "repeats_per_task": 1,
            "paired_task_count": run_count,
            "paired_task_set_same": True,
            "configurations": results,
            "comparison": _comparison_result(comparison),
            "validity_warnings": [],
        },
        "finding": {"conclusion": "insufficient_evidence"},
        "support": {
            "events": [
                {
                    "sequence": 1,
                    "actor_role": "provider_support",
                    "kind": "published_runbook",
                }
            ]
        },
    }


def _configurations(experiment: Mapping[str, Any]) -> list[dict[str, str]]:
    raw_configs = experiment.get("configs")
    if not isinstance(raw_configs, list) or len(raw_configs) != 2:
        raise EnterpriseHarnessError("experiment must contain exactly two configs")
    results: list[dict[str, str]] = []
    for expected, raw in zip(_CONFIGURATION_IDS, raw_configs, strict=True):
        if not isinstance(raw, dict) or raw.get("label") != expected:
            raise EnterpriseHarnessError("experiment config labels must be A then B")
        encoded = json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()
        results.append(
            {
                "configuration_id": expected,
                "configuration_digest": _digest(encoded),
            }
        )
    return results


def _summaries(report: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = report.get("summaries")
    if not isinstance(raw, list) or len(raw) != 2:
        raise EnterpriseHarnessError("interpret report must contain two summaries")
    summaries: dict[str, Mapping[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict) or item.get("label") not in _CONFIGURATION_IDS:
            raise EnterpriseHarnessError("interpret summaries do not match A and B")
        label = str(item["label"])
        if label in summaries:
            raise EnterpriseHarnessError("interpret summaries contain duplicates")
        summaries[label] = item
    if tuple(summaries) != _CONFIGURATION_IDS:
        raise EnterpriseHarnessError("interpret summaries must be ordered A then B")
    return summaries


def _paired_task_ids(report: Mapping[str, Any]) -> tuple[str, ...]:
    raw_tasks = report.get("tasks")
    if not isinstance(raw_tasks, list):
        raise EnterpriseHarnessError("interpret report has no task rows")
    task_sets: dict[str, set[str]] = {
        config_id: set() for config_id in _CONFIGURATION_IDS
    }
    for task in raw_tasks:
        if not isinstance(task, dict):
            raise EnterpriseHarnessError("interpret task row is malformed")
        config = task.get("config")
        task_id = task.get("task_id")
        if config in task_sets and isinstance(task_id, str) and task_id:
            task_sets[config].add(task_id)
    paired = tuple(sorted(task_sets["A"] & task_sets["B"]))
    if not paired:
        raise EnterpriseHarnessError("interpret report has no paired task")
    return paired


def _comparison(report: Mapping[str, Any]) -> Mapping[str, Any]:
    raw = report.get("comparisons")
    if not isinstance(raw, list) or len(raw) != 1 or not isinstance(raw[0], dict):
        raise EnterpriseHarnessError("interpret report must contain one comparison")
    return raw[0]


def _configuration_result(
    config_id: str,
    summary: Mapping[str, Any],
    paired_count: int,
) -> dict[str, Any]:
    total = _integer(summary, "total_tasks")
    errored = _integer(summary, "errored_count")
    if total - errored != paired_count:
        raise EnterpriseHarnessError("run counts do not match paired task evidence")
    cost = summary.get("total_cost_usd")
    if not _is_number(cost):
        raise EnterpriseHarnessError("real-agent cost telemetry is unavailable")
    cost_coverage = _number(summary, "cost_coverage")
    if cost_coverage != 1.0:
        raise EnterpriseHarnessError("real-agent cost telemetry is incomplete")
    return {
        "configuration_id": config_id,
        "scorable_run_count": paired_count,
        "total_run_count": total,
        "mean_quality": _number(summary, "mean_score"),
        "quality_ci": {
            "lower": _number(summary, "ci_lower"),
            "upper": _number(summary, "ci_upper"),
        },
        "total_cost_usd": float(cost),
        "cost_coverage": cost_coverage,
        "mean_latency_seconds": _number(summary, "mean_duration_sec"),
        "total_latency_seconds": _number(summary, "total_duration_sec"),
    }


def _sample(paired_tasks: Sequence[str]) -> dict[str, Any]:
    task_pairs = [
        {
            "task_digest": _digest(f"task:{task_id}".encode()),
            "verifier_digest": _digest(f"verifier:{task_id}".encode()),
            "category_id": _SYNTHETIC_CATEGORY,
        }
        for task_id in paired_tasks
    ]
    count = len(task_pairs)
    return {
        "window": {"start": "2026-01-01", "end": "2026-01-31"},
        "selection_method": "predeclared_explicit",
        "changed_after_results": False,
        "task_pairs": task_pairs,
        "category_counts": [
            {
                "category_id": _SYNTHETIC_CATEGORY,
                "selected_count": count,
                "paired_scorable_count": count,
            }
        ],
        "exclusions": [],
        "attrition_count": 0,
        "representative": False,
    }


def _comparison_result(comparison: Mapping[str, Any]) -> dict[str, Any]:
    comparable = comparison.get("comparable") is True
    method = comparison.get("effect_size_method")
    return {
        "report_comparable": comparable,
        "report_refusal_code": None if comparable else "other_structural_refusal",
        "score_difference": _number(comparison, "score_diff"),
        "cost_difference_usd": _optional_number(comparison.get("cost_diff")),
        "latency_difference_seconds": _number(comparison, "speed_diff"),
        "p_value": _optional_number(comparison.get("p_value")),
        "effect_size": _optional_number(comparison.get("effect_size")),
        "effect_size_method": method if method else "none",
        "confidence_interval": {
            "lower": _number(comparison, "ci_lower"),
            "upper": _number(comparison, "ci_upper"),
        },
    }


def _digest(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _integer(value: Mapping[str, Any], field: str) -> int:
    raw = value.get(field)
    if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
        raise EnterpriseHarnessError(f"{field} must be a non-negative integer")
    return raw


def _number(value: Mapping[str, Any], field: str) -> float:
    raw = value.get(field)
    if not _is_number(raw):
        raise EnterpriseHarnessError(f"{field} must be a finite number")
    return float(raw)


def _optional_number(raw: Any) -> float | None:
    if raw is None:
        return None
    if not _is_number(raw):
        raise EnterpriseHarnessError("comparison metric must be a finite number")
    return float(raw)


def _is_number(raw: Any) -> TypeGuard[int | float]:
    return (
        not isinstance(raw, bool)
        and isinstance(raw, (int, float))
        and math.isfinite(float(raw))
    )


__all__ = [
    "EnterpriseHarnessError",
    "assert_no_secret_values",
    "build_evidence_request",
    "parse_envelope",
    "validate_image_labels",
]
