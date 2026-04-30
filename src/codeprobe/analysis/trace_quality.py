"""Trace-quality metrics — derive per-task and aggregate quality signals.

This module turns the structured outputs codeprobe already records (run
status, error category, scoring details, bias warnings) into a single
quality view that surfaces in ``aggregate.json`` under
``quality_metrics``.

Design notes
------------

* **ZFC-compliant by construction.** Every signal is a structural fact
  read from a typed field — no semantic judgment, no hardcoded thresholds
  for meaning detection. The single threshold that appears
  (:data:`LOW_RECALL_THRESHOLD`) is a documented constant that simply
  surfaces an existing oracle metric, not a quality verdict.

* **Adapter pattern.** The codeprobe-native path
  (:meth:`TraceQualityReporter.from_completed_tasks`) consumes
  :class:`CompletedTask` records plus a list of
  :class:`~codeprobe.core.bias_detection.BiasWarning` records. External
  benchmarks (EnterpriseBench, CodeScaleBench) plug in by building
  :class:`TraceQualityMetrics` rows from their own result shapes and
  feeding them into :meth:`TraceQualityReporter.from_metrics` — no
  benchmark-specific logic lives here.

* **Schema versioned.** The on-the-wire ``quality_metrics`` block in
  ``aggregate.json`` carries an explicit ``schema_version`` so
  downstream tooling can detect breaking changes without sniffing keys.

Validity model
--------------

A *trial* is one (config, task, repeat_index) tuple — i.e. one
:class:`CompletedTask` row. Validity collapses the existing ``status``
and ``error_category`` fields:

* ``valid``   — ``status == "completed"``: scoring ran end-to-end. The
  trial may still have failed the scoring oracle; that's a score signal,
  not a validity signal.
* ``invalid`` — ``status == "error"``: the trial never produced a
  meaningful result. ``validity_reason`` carries the existing
  ``error_category`` (``timeout``, ``system``, ``agent``) or the
  fallback string ``"unknown"`` when the executor recorded an error
  without classification.

Quality flags layer on top of validity:

* ``invalid``           — one per invalid trial; mirrors validity for
  flag-level aggregation.
* ``timeout`` /
  ``system_error`` /
  ``agent_error``       — direct mapping of ``error_category`` so flag
  histograms read like the existing executor stderr counts without a
  separate query.
* ``scorer_error``      — ``status == "completed"`` but
  ``scoring_details["error"]`` is non-empty. The trial completed but
  the scorer itself failed (test.sh missing, sandbox crash, oracle
  parse error).
* ``low_recall``        — oracle scoring exposed
  ``scoring_details["recall"]`` and the value is below
  :data:`LOW_RECALL_THRESHOLD`. Surfaces the existing structural metric
  so it shows up in the quality histogram alongside validity flags.
* bias-warning flags    — every :class:`BiasWarning` whose ``detail``
  carries a ``task_id`` or ``affected_tasks`` is fanned out to the
  matching per-task rows so aggregate-time bias detection contributes
  to per-trial quality. Experiment-level warnings
  (``no_independent_baseline``) are recorded on the overall summary
  rather than fanned out, since they apply to the whole comparison.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from codeprobe.core.bias_detection import BiasWarning
from codeprobe.models.experiment import CompletedTask

SCHEMA_VERSION = 1

# Recall threshold below which an oracle-scored task is flagged
# ``low_recall``. Mirrors the existing structural oracle metric — not a
# semantic quality verdict. Tunable per-experiment via
# :class:`TraceQualityReporter` constructor argument.
LOW_RECALL_THRESHOLD = 0.5


@dataclass(frozen=True)
class TraceQualityMetrics:
    """Per-trial quality view derived from a :class:`CompletedTask`.

    Adapter callers may construct instances directly when adapting
    benchmark formats that don't produce ``CompletedTask`` rows; the
    only contract is that ``quality_flags`` is sorted and unique so
    aggregation is deterministic.
    """

    task_id: str
    config_label: str
    repeat_index: int = 0
    validity: str = "valid"
    validity_reason: str | None = None
    score: float | None = None
    scorer_passed: bool | None = None
    precision: float | None = None
    recall: float | None = None
    f1: float | None = None
    quality_flags: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_invalid(self) -> bool:
        return self.validity == "invalid"

    @property
    def is_low_quality(self) -> bool:
        return self.is_invalid or bool(self.quality_flags)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["quality_flags"] = list(self.quality_flags)
        d["detail"] = dict(self.detail)
        return d


@dataclass(frozen=True)
class TraceQualitySummary:
    """Aggregate quality view across one config (or the full experiment)."""

    scope: str
    total_trials: int
    valid_trials: int
    invalid_trials: int
    low_quality_trials: int
    flag_counts: Mapping[str, int]
    error_category_counts: Mapping[str, int]
    experiment_warnings: tuple[str, ...] = ()

    @property
    def invalid_rate(self) -> float:
        return self.invalid_trials / self.total_trials if self.total_trials else 0.0

    @property
    def low_quality_rate(self) -> float:
        return self.low_quality_trials / self.total_trials if self.total_trials else 0.0

    @property
    def valid_rate(self) -> float:
        return self.valid_trials / self.total_trials if self.total_trials else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "total_trials": self.total_trials,
            "valid_trials": self.valid_trials,
            "invalid_trials": self.invalid_trials,
            "low_quality_trials": self.low_quality_trials,
            "invalid_rate": round(self.invalid_rate, 4),
            "valid_rate": round(self.valid_rate, 4),
            "low_quality_rate": round(self.low_quality_rate, 4),
            "flag_counts": dict(sorted(self.flag_counts.items())),
            "error_category_counts": dict(sorted(self.error_category_counts.items())),
            "experiment_warnings": list(self.experiment_warnings),
        }


_ERROR_CATEGORY_FLAGS: Mapping[str, str] = {
    "timeout": "timeout",
    "system": "system_error",
    "agent": "agent_error",
}


def _validity_from_task(task: CompletedTask) -> tuple[str, str | None]:
    """Map a CompletedTask onto (validity, validity_reason)."""
    if task.status == "completed":
        return "valid", None
    return "invalid", task.error_category or "unknown"


def _scoring_metric(details: Mapping[str, Any], key: str) -> float | None:
    value = details.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def _bias_warning_flags(
    warnings: Sequence[BiasWarning],
) -> tuple[dict[str, dict[str, list[str]]], list[str]]:
    """Project bias warnings onto per-task flag rows + experiment-level flags.

    Returns ``(per_task, experiment_level)`` where:

    * ``per_task[task_id][config_label]`` is the list of bias flags that
      apply to that specific (task, config) pair. ``config_label`` may
      be ``"*"`` for warnings that don't carry a config attribution
      (e.g. overshipping records both labels separately, but
      backend_overlap binds to a single config).
    * ``experiment_level`` is the list of warning kinds that apply to
      the comparison as a whole.
    """
    per_task: dict[str, dict[str, list[str]]] = {}
    experiment_level: list[str] = []

    def _bind(task_id: str, config: str, kind: str) -> None:
        bucket = per_task.setdefault(task_id, {}).setdefault(config, [])
        if kind not in bucket:
            bucket.append(kind)

    for warning in warnings:
        kind = warning.kind
        detail = warning.detail or {}
        if kind == "overshipping":
            tid = detail.get("task_id")
            loser = detail.get("loser_config")
            winner = detail.get("winner_config")
            if isinstance(tid, str):
                if isinstance(loser, str):
                    _bind(tid, loser, kind)
                if isinstance(winner, str):
                    _bind(tid, winner, kind)
        elif kind == "backend_overlap":
            cfg = detail.get("config")
            affected = detail.get("affected_tasks")
            severity = getattr(warning, "severity", "warning")
            # Severity-suffixed flag (codeprobe-9re9): dashboards filter
            # to true tautology risks via ``backend_overlap``; the
            # ``backend_overlap_informational`` flag carries the
            # independent-corroboration variant separately.
            flag_name = (
                "backend_overlap_informational"
                if severity == "informational"
                else "backend_overlap"
            )
            if isinstance(cfg, str) and isinstance(affected, list):
                for tid in affected:
                    if isinstance(tid, str):
                        _bind(tid, cfg, flag_name)
        elif kind == "no_independent_baseline":
            experiment_level.append(kind)
        else:
            experiment_level.append(kind)

    return per_task, experiment_level


def _build_metrics_from_task(
    task: CompletedTask,
    config_label: str,
    extra_flags: Sequence[str] = (),
    low_recall_threshold: float = LOW_RECALL_THRESHOLD,
) -> TraceQualityMetrics:
    validity, reason = _validity_from_task(task)
    details = task.scoring_details or {}

    flags: list[str] = []
    if validity == "invalid":
        flags.append("invalid")
        if reason and reason in _ERROR_CATEGORY_FLAGS:
            flags.append(_ERROR_CATEGORY_FLAGS[reason])

    if validity == "valid":
        scorer_error = details.get("error")
        if isinstance(scorer_error, str) and scorer_error.strip():
            flags.append("scorer_error")

    recall = _scoring_metric(details, "recall")
    if recall is not None and recall < low_recall_threshold:
        flags.append("low_recall")

    for extra in extra_flags:
        if extra and extra not in flags:
            flags.append(extra)

    scorer_passed_raw = details.get("passed")
    if isinstance(scorer_passed_raw, bool):
        scorer_passed: bool | None = scorer_passed_raw
    else:
        scorer_passed = None

    detail: dict[str, Any] = {}
    scoring_error = details.get("error")
    if isinstance(scoring_error, str) and scoring_error.strip():
        detail["scoring_error"] = scoring_error
    if validity == "invalid":
        meta_error = (task.metadata or {}).get("error")
        if isinstance(meta_error, str) and meta_error.strip():
            detail["task_error"] = meta_error

    return TraceQualityMetrics(
        task_id=task.task_id,
        config_label=config_label,
        repeat_index=task.repeat_index,
        validity=validity,
        validity_reason=reason,
        score=task.automated_score if validity == "valid" else None,
        scorer_passed=scorer_passed,
        precision=_scoring_metric(details, "precision"),
        recall=recall,
        f1=_scoring_metric(details, "f1"),
        quality_flags=tuple(sorted(set(flags))),
        detail=detail,
    )


def _summarize(
    scope: str,
    metrics: Iterable[TraceQualityMetrics],
    experiment_warnings: Sequence[str] = (),
) -> TraceQualitySummary:
    total = 0
    valid = 0
    invalid = 0
    low_quality = 0
    flag_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    for m in metrics:
        total += 1
        if m.is_invalid:
            invalid += 1
            if m.validity_reason:
                error_counts[m.validity_reason] = (
                    error_counts.get(m.validity_reason, 0) + 1
                )
        else:
            valid += 1
        if m.is_low_quality:
            low_quality += 1
        for flag in m.quality_flags:
            flag_counts[flag] = flag_counts.get(flag, 0) + 1
    return TraceQualitySummary(
        scope=scope,
        total_trials=total,
        valid_trials=valid,
        invalid_trials=invalid,
        low_quality_trials=low_quality,
        flag_counts=flag_counts,
        error_category_counts=error_counts,
        experiment_warnings=tuple(dict.fromkeys(experiment_warnings)),
    )


class TraceQualityReporter:
    """Aggregate per-trial quality metrics for ``aggregate.json``.

    The reporter is built once per experiment-aggregate call. It owns
    the metrics list and exposes per-config / overall summary views and
    a serializable :meth:`to_dict` for direct inclusion in
    ``aggregate.json`` under ``quality_metrics``.
    """

    def __init__(
        self,
        metrics: Sequence[TraceQualityMetrics] | None = None,
        experiment_warnings: Sequence[str] = (),
        schema_version: int = SCHEMA_VERSION,
    ) -> None:
        self._metrics: list[TraceQualityMetrics] = list(metrics or [])
        self._experiment_warnings: tuple[str, ...] = tuple(
            dict.fromkeys(experiment_warnings)
        )
        self._schema_version = schema_version

    @classmethod
    def from_completed_tasks(
        cls,
        config_results: Mapping[str, Sequence[CompletedTask]],
        bias_warnings: Sequence[BiasWarning] = (),
        low_recall_threshold: float = LOW_RECALL_THRESHOLD,
    ) -> "TraceQualityReporter":
        """Build a reporter from codeprobe-native results.

        ``config_results`` maps config label → completed task records,
        matching the shape produced by
        :func:`codeprobe.core.experiment.load_config_results` and the
        intermediate dict assembled inside ``experiment_aggregate``.
        """
        per_task_flags, experiment_warnings = _bias_warning_flags(bias_warnings)

        metrics: list[TraceQualityMetrics] = []
        for cfg_label, tasks in config_results.items():
            for task in tasks:
                extra = per_task_flags.get(task.task_id, {}).get(cfg_label, [])
                metrics.append(
                    _build_metrics_from_task(
                        task,
                        cfg_label,
                        extra_flags=extra,
                        low_recall_threshold=low_recall_threshold,
                    )
                )

        return cls(metrics=metrics, experiment_warnings=experiment_warnings)

    @classmethod
    def from_metrics(
        cls,
        metrics: Sequence[TraceQualityMetrics],
        experiment_warnings: Sequence[str] = (),
    ) -> "TraceQualityReporter":
        """Adapter entry point — wrap pre-built metrics rows.

        External benchmark adapters (EnterpriseBench, CodeScaleBench)
        construct :class:`TraceQualityMetrics` from their own result
        shapes and pass the rows through here to inherit summary,
        low-quality iteration, and serialization without depending on
        :class:`CompletedTask`.
        """
        return cls(metrics=metrics, experiment_warnings=experiment_warnings)

    @property
    def metrics(self) -> tuple[TraceQualityMetrics, ...]:
        return tuple(self._metrics)

    def low_quality(self) -> list[TraceQualityMetrics]:
        return [m for m in self._metrics if m.is_low_quality]

    def per_config_summary(self) -> dict[str, TraceQualitySummary]:
        by_label: dict[str, list[TraceQualityMetrics]] = {}
        for m in self._metrics:
            by_label.setdefault(m.config_label, []).append(m)
        return {
            label: _summarize(label, rows) for label, rows in sorted(by_label.items())
        }

    def overall_summary(self) -> TraceQualitySummary:
        return _summarize("overall", self._metrics, self._experiment_warnings)

    def to_dict(self) -> dict[str, Any]:
        """Serializable view for ``aggregate.json[quality_metrics]``."""
        return {
            "schema_version": self._schema_version,
            "overall": self.overall_summary().to_dict(),
            "per_config": {
                label: summary.to_dict()
                for label, summary in self.per_config_summary().items()
            },
            "low_quality_trials": [m.to_dict() for m in self.low_quality()],
        }


__all__ = [
    "LOW_RECALL_THRESHOLD",
    "SCHEMA_VERSION",
    "TraceQualityMetrics",
    "TraceQualityReporter",
    "TraceQualitySummary",
]
