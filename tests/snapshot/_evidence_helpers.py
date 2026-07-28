"""Fixtures for zero-code-access evidence-bundle tests."""

from __future__ import annotations

import hashlib
from typing import Any

from codeprobe import __version__


def digest(label: str) -> str:
    """Return a deterministic prefixed SHA-256 digest for test data."""
    return f"sha256:{hashlib.sha256(label.encode()).hexdigest()}"


def _task_pairs(task_count: int) -> list[dict[str, Any]]:
    return [
        {
            "task_digest": digest(f"task-{index}"),
            "verifier_digest": digest(f"verifier-{index}"),
            "category_id": "category_01",
        }
        for index in range(task_count)
    ]


def _sample(
    task_count: int,
    changed_after_results: bool,
) -> dict[str, Any]:
    return {
        "window": {"start": "2026-01-01", "end": "2026-06-30"},
        "selection_method": "predeclared_stratified",
        "changed_after_results": changed_after_results,
        "task_pairs": _task_pairs(task_count),
        "category_counts": [
            {
                "category_id": "category_01",
                "selected_count": task_count,
                "paired_scorable_count": task_count,
            }
        ],
        "exclusions": [{"reason": "predeclared", "count": 2}],
        "attrition_count": 0,
        "representative": True,
    }


def _configuration_result(
    configuration_id: str,
    *,
    run_count: int,
    mean_quality: float,
    quality_interval: tuple[float, float],
    total_cost_usd: float,
    mean_latency_seconds: float,
    total_latency_seconds: float,
) -> dict[str, Any]:
    return {
        "configuration_id": configuration_id,
        "scorable_run_count": run_count,
        "total_run_count": run_count,
        "mean_quality": mean_quality,
        "quality_ci": {
            "lower": quality_interval[0],
            "upper": quality_interval[1],
        },
        "total_cost_usd": total_cost_usd,
        "cost_coverage": 1.0,
        "mean_latency_seconds": mean_latency_seconds,
        "total_latency_seconds": total_latency_seconds,
    }


def _results(
    task_count: int,
    repeats: int,
    same_task_set: bool,
) -> dict[str, Any]:
    run_count = task_count * repeats
    return {
        "quality_metric": "mean_score",
        "repeats_per_task": repeats,
        "paired_task_count": task_count,
        "paired_task_set_same": same_task_set,
        "configurations": [
            _configuration_result(
                "A",
                run_count=run_count,
                mean_quality=0.72,
                quality_interval=(0.62, 0.82),
                total_cost_usd=12.5,
                mean_latency_seconds=42.0,
                total_latency_seconds=1260.0,
            ),
            _configuration_result(
                "B",
                run_count=run_count,
                mean_quality=0.61,
                quality_interval=(0.51, 0.71),
                total_cost_usd=10.0,
                mean_latency_seconds=39.0,
                total_latency_seconds=1170.0,
            ),
        ],
        "comparison": {
            "report_comparable": True,
            "report_refusal_code": None,
            "score_difference": 0.11,
            "cost_difference_usd": 2.5,
            "latency_difference_seconds": 3.0,
            "p_value": 0.04,
            "effect_size": 0.35,
            "effect_size_method": "cohens_d",
            "confidence_interval": {"lower": 0.01, "upper": 0.21},
        },
        "validity_warnings": [],
    }


def evidence_request(
    *,
    conclusion: str = "advance_a",
    task_count: int = 10,
    repeats: int = 3,
    same_task_set: bool = True,
    changed_after_results: bool = False,
    support_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build one structurally valid data-owner-controlled bundle request."""
    return {
        "schema_version": "codeprobe.zero-code-access.request.v1",
        "run": {
            "codeprobe_version": __version__,
            "environment": {
                "execution_location": "data_owner_environment",
                "repository_access": "data_owner_only",
                "network_posture": "restricted",
            },
            "configurations": [
                {
                    "configuration_id": "A",
                    "configuration_digest": digest("configuration-a"),
                },
                {
                    "configuration_id": "B",
                    "configuration_digest": digest("configuration-b"),
                },
            ],
        },
        "sample": _sample(task_count, changed_after_results),
        "results": _results(task_count, repeats, same_task_set),
        "finding": {"conclusion": conclusion},
        "support": {
            "events": support_events
            if support_events is not None
            else [
                {
                    "sequence": 1,
                    "actor_role": "provider_support",
                    "kind": "published_runbook",
                }
            ]
        },
    }
