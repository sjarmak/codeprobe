"""Unit tests for ``codeprobe.analysis.trace_quality``."""

from __future__ import annotations

import json

import pytest

from codeprobe.analysis.trace_quality import (
    LOW_RECALL_THRESHOLD,
    SCHEMA_VERSION,
    TraceQualityMetrics,
    TraceQualityReporter,
    _bias_warning_flags,
    _build_metrics_from_task,
)
from codeprobe.core.bias_detection import BiasWarning
from codeprobe.models.experiment import CompletedTask


def _ok(task_id: str, *, score: float = 1.0, details: dict | None = None) -> CompletedTask:
    return CompletedTask(
        task_id=task_id,
        automated_score=score,
        status="completed",
        scoring_details=details if details is not None else {"passed": True, "error": None},
    )


def _err(
    task_id: str,
    *,
    error_category: str | None = "agent",
    metadata: dict | None = None,
) -> CompletedTask:
    return CompletedTask(
        task_id=task_id,
        automated_score=0.0,
        status="error",
        error_category=error_category,
        metadata=metadata or {},
    )


class TestPerTaskBuilder:
    """``_build_metrics_from_task`` covers the codeprobe-native projection."""

    def test_completed_task_marks_valid_and_propagates_oracle_metrics(self) -> None:
        task = _ok(
            "t1",
            details={
                "passed": True,
                "error": None,
                "precision": 0.9,
                "recall": 0.8,
                "f1": 0.85,
            },
        )

        m = _build_metrics_from_task(task, "baseline")

        assert m.task_id == "t1"
        assert m.config_label == "baseline"
        assert m.validity == "valid"
        assert m.validity_reason is None
        assert m.is_invalid is False
        assert m.is_low_quality is False
        assert m.precision == pytest.approx(0.9)
        assert m.recall == pytest.approx(0.8)
        assert m.f1 == pytest.approx(0.85)
        assert m.scorer_passed is True
        assert m.score == pytest.approx(1.0)
        assert m.quality_flags == ()

    def test_error_task_collapses_to_invalid_with_error_category_flag(self) -> None:
        task = _err("t-timeout", error_category="timeout", metadata={"error": "boom"})

        m = _build_metrics_from_task(task, "baseline")

        assert m.validity == "invalid"
        assert m.validity_reason == "timeout"
        assert m.score is None
        # Both ``invalid`` and the category-specific flag are emitted, sorted.
        assert "invalid" in m.quality_flags
        assert "timeout" in m.quality_flags
        assert m.detail.get("task_error") == "boom"

    def test_unknown_error_category_falls_back_to_unknown(self) -> None:
        task = _err("t-mystery", error_category=None)

        m = _build_metrics_from_task(task, "baseline")

        assert m.validity == "invalid"
        assert m.validity_reason == "unknown"
        # Unknown categories don't produce a category-specific flag, only the
        # generic ``invalid`` one — keeps flag histograms honest.
        assert m.quality_flags == ("invalid",)

    def test_completed_but_scorer_errored_emits_scorer_error_flag(self) -> None:
        task = _ok(
            "t-scorer",
            score=0.0,
            details={"passed": False, "error": "tests/test.sh not found"},
        )

        m = _build_metrics_from_task(task, "baseline")

        assert m.validity == "valid"
        assert "scorer_error" in m.quality_flags
        assert m.detail["scoring_error"] == "tests/test.sh not found"

    def test_low_recall_flag_uses_threshold(self) -> None:
        task_low = _ok(
            "t-lr",
            details={"passed": True, "error": None, "recall": 0.3},
        )
        task_high = _ok(
            "t-hr",
            details={"passed": True, "error": None, "recall": 0.9},
        )

        m_low = _build_metrics_from_task(task_low, "baseline")
        m_high = _build_metrics_from_task(task_high, "baseline")

        assert "low_recall" in m_low.quality_flags
        assert "low_recall" not in m_high.quality_flags
        # Threshold is configurable so callers can flip the boundary
        # without monkey-patching the module constant.
        m_loose = _build_metrics_from_task(
            task_high, "baseline", low_recall_threshold=0.95
        )
        assert "low_recall" in m_loose.quality_flags

    def test_low_recall_threshold_default_constant(self) -> None:
        # Trip a scenario right at the boundary; equal-to threshold is OK.
        boundary = _ok(
            "t-edge",
            details={"passed": True, "error": None, "recall": LOW_RECALL_THRESHOLD},
        )
        m = _build_metrics_from_task(boundary, "baseline")
        assert "low_recall" not in m.quality_flags

    def test_extra_flags_merge_and_dedupe(self) -> None:
        task = _ok("t-extra")

        m = _build_metrics_from_task(
            task,
            "baseline",
            extra_flags=("backend_overlap", "backend_overlap"),
        )

        assert m.quality_flags == ("backend_overlap",)


class TestBiasWarningProjection:
    """``_bias_warning_flags`` fans bias warnings to per-task rows."""

    def test_overshipping_warning_binds_to_both_configs(self) -> None:
        warnings = [
            BiasWarning(
                kind="overshipping",
                message="...",
                detail={
                    "task_id": "t1",
                    "loser_config": "baseline",
                    "winner_config": "with-mcp",
                    "loser_score": 0.0,
                    "winner_score": 1.0,
                    "loser_recall": 1.0,
                },
            )
        ]

        per_task, experiment_level = _bias_warning_flags(warnings)

        assert experiment_level == []
        assert per_task["t1"]["baseline"] == ["overshipping"]
        assert per_task["t1"]["with-mcp"] == ["overshipping"]

    def test_backend_overlap_binds_to_single_config_and_all_tasks(self) -> None:
        warnings = [
            BiasWarning(
                kind="backend_overlap",
                message="...",
                detail={
                    "config": "with-mcp",
                    "shared_backends": ["sourcegraph"],
                    "affected_task_count": 2,
                    "affected_tasks": ["t1", "t2"],
                },
            )
        ]

        per_task, experiment_level = _bias_warning_flags(warnings)

        assert experiment_level == []
        assert per_task["t1"]["with-mcp"] == ["backend_overlap"]
        assert per_task["t2"]["with-mcp"] == ["backend_overlap"]

    def test_no_independent_baseline_is_experiment_level_only(self) -> None:
        warnings = [
            BiasWarning(
                kind="no_independent_baseline",
                message="...",
                detail={"sole_backend": "sourcegraph"},
            )
        ]

        per_task, experiment_level = _bias_warning_flags(warnings)

        assert per_task == {}
        assert experiment_level == ["no_independent_baseline"]


class TestReporter:
    """End-to-end behaviour of :class:`TraceQualityReporter`."""

    def test_from_completed_tasks_summary_counts_match(self) -> None:
        config_results = {
            "baseline": [
                _ok("t1"),
                _err("t2", error_category="timeout"),
                _ok(
                    "t3",
                    score=0.0,
                    details={"passed": False, "error": "scorer crashed"},
                ),
            ],
            "with-mcp": [
                _ok("t1"),
                _ok("t2"),
                _ok("t3"),
            ],
        }
        warnings = [
            BiasWarning(
                kind="backend_overlap",
                message="...",
                detail={
                    "config": "with-mcp",
                    "shared_backends": ["sourcegraph"],
                    "affected_task_count": 3,
                    "affected_tasks": ["t1", "t2", "t3"],
                },
            ),
            BiasWarning(
                kind="no_independent_baseline",
                message="...",
                detail={"sole_backend": "sourcegraph"},
            ),
        ]

        reporter = TraceQualityReporter.from_completed_tasks(config_results, warnings)
        overall = reporter.overall_summary()

        assert overall.total_trials == 6
        assert overall.invalid_trials == 1
        assert overall.valid_trials == 5
        # invalid + scorer_error in baseline + 3 backend_overlap in with-mcp.
        assert overall.low_quality_trials == 1 + 1 + 3
        assert overall.flag_counts.get("invalid") == 1
        assert overall.flag_counts.get("timeout") == 1
        assert overall.flag_counts.get("scorer_error") == 1
        assert overall.flag_counts.get("backend_overlap") == 3
        assert overall.error_category_counts.get("timeout") == 1
        # Experiment-level bias warning rides on the overall summary.
        assert "no_independent_baseline" in overall.experiment_warnings

    def test_per_config_summaries_partition_metrics(self) -> None:
        config_results = {
            "baseline": [_ok("t1"), _err("t2", error_category="agent")],
            "with-mcp": [_ok("t1"), _ok("t2")],
        }

        reporter = TraceQualityReporter.from_completed_tasks(config_results)
        per_config = reporter.per_config_summary()

        assert set(per_config.keys()) == {"baseline", "with-mcp"}
        assert per_config["baseline"].total_trials == 2
        assert per_config["baseline"].invalid_trials == 1
        assert per_config["with-mcp"].invalid_trials == 0

    def test_low_quality_iter_excludes_clean_trials(self) -> None:
        config_results = {
            "baseline": [_ok("t1"), _err("t2", error_category="system")],
        }

        reporter = TraceQualityReporter.from_completed_tasks(config_results)
        low = reporter.low_quality()

        assert [m.task_id for m in low] == ["t2"]
        assert low[0].validity_reason == "system"
        assert "system_error" in low[0].quality_flags

    def test_to_dict_round_trips_through_json(self) -> None:
        config_results = {
            "baseline": [_ok("t1"), _err("t2", error_category="timeout")],
        }

        reporter = TraceQualityReporter.from_completed_tasks(config_results)
        payload = reporter.to_dict()
        # round-tripping through JSON is the contract the aggregate.json
        # caller relies on — surface any non-JSON-safe value early.
        encoded = json.dumps(payload)
        decoded = json.loads(encoded)

        assert decoded["schema_version"] == SCHEMA_VERSION
        assert decoded["overall"]["total_trials"] == 2
        assert "baseline" in decoded["per_config"]
        assert any(
            row["task_id"] == "t2" for row in decoded["low_quality_trials"]
        )

    def test_empty_results_summarize_to_zero(self) -> None:
        reporter = TraceQualityReporter.from_completed_tasks({})
        overall = reporter.overall_summary()

        assert overall.total_trials == 0
        assert overall.invalid_rate == 0.0
        assert overall.valid_rate == 0.0
        assert overall.low_quality_rate == 0.0
        assert reporter.to_dict()["per_config"] == {}

    def test_from_metrics_adapter_path_preserves_experiment_warnings(self) -> None:
        rows = [
            TraceQualityMetrics(
                task_id="t1",
                config_label="baseline",
                validity="invalid",
                validity_reason="timeout",
                quality_flags=("invalid", "timeout"),
            )
        ]

        reporter = TraceQualityReporter.from_metrics(
            rows, experiment_warnings=("custom_warning",)
        )
        overall = reporter.overall_summary()

        assert overall.total_trials == 1
        assert overall.experiment_warnings == ("custom_warning",)
        assert overall.flag_counts.get("invalid") == 1
        assert overall.flag_counts.get("timeout") == 1
