# Trace Quality Reporter

`TraceQualityReporter` consolidates the per-trial quality signals codeprobe
already records (run status, error category, scoring details, bias
warnings) into one structured view that surfaces in `aggregate.json` under
`quality_metrics`. It also serves as the abstraction other benchmarks
(EnterpriseBench, CodeScaleBench) can adapt to so quality reporting stays
shape-compatible across rigs.

## Why

Before this module each benchmark grew its own quality view:

* **codeprobe** had `bias_warnings[]` (experiment-level tautology /
  capability-boundary detection) but no per-trial validity histogram.
* **CodeScaleBench** carried a partially-implemented
  `scripts/csb_metrics/trace_quality.py` with three stages (validity,
  setup, hallucination) and a `get_summary()` parsing bug.
* **EnterpriseBench** records per-task `success`, `phase`, `failure_class`,
  and `scores.checkpoints[]` but never aggregates them into a quality
  summary.

Rather than fix CSB's reporter in place (CSB is currently suspended), the
pattern is abstracted into codeprobe — the most active benchmark — and
exposed as a stable schema other rigs adapt to.

## Schema (v1)

```jsonc
// aggregate.json snippet
{
  "quality_metrics": {
    "schema_version": 1,
    "overall": {
      "scope": "overall",
      "total_trials": 24,
      "valid_trials": 20,
      "invalid_trials": 4,
      "low_quality_trials": 6,
      "invalid_rate": 0.1667,
      "valid_rate": 0.8333,
      "low_quality_rate": 0.25,
      "flag_counts": {
        "backend_overlap": 2,
        "invalid": 4,
        "low_recall": 1,
        "scorer_error": 1,
        "timeout": 3
      },
      "error_category_counts": {"agent": 1, "system": 0, "timeout": 3},
      "experiment_warnings": ["no_independent_baseline"]
    },
    "per_config": {
      "baseline":  { "scope": "baseline",  /* ...same shape as overall... */ },
      "with-mcp":  { "scope": "with-mcp",  /* ...same shape as overall... */ }
    },
    "low_quality_trials": [
      {
        "task_id": "task-001",
        "config_label": "baseline",
        "repeat_index": 0,
        "validity": "invalid",
        "validity_reason": "timeout",
        "score": null,
        "scorer_passed": null,
        "precision": null,
        "recall": null,
        "f1": null,
        "quality_flags": ["invalid", "timeout"],
        "detail": {"task_error": "subprocess timed out"}
      }
    ]
  }
}
```

### Validity model

A *trial* is one `(config, task_id, repeat_index)` tuple — i.e. one
`CompletedTask` row. Validity collapses the existing `status` and
`error_category` fields:

| `status`      | `error_category` | `validity` | `validity_reason`         |
| ------------- | ---------------- | ---------- | ------------------------- |
| `completed`   | (any)            | `valid`    | `null`                    |
| `error`       | `timeout`        | `invalid`  | `timeout`                 |
| `error`       | `system`         | `invalid`  | `system`                  |
| `error`       | `agent`          | `invalid`  | `agent`                   |
| `error`       | `null`           | `invalid`  | `unknown`                 |

A *valid* trial may still fail the scoring oracle — that is a score
signal (`score`, `scorer_passed`), not a validity signal.

### Quality flags

Flags layer on top of validity. They are mechanical projections of
existing fields — no semantic judgment:

| Flag                | Source                                                       |
| ------------------- | ------------------------------------------------------------ |
| `invalid`           | `validity == "invalid"`                                      |
| `timeout`           | `error_category == "timeout"`                                |
| `system_error`      | `error_category == "system"`                                 |
| `agent_error`       | `error_category == "agent"`                                  |
| `scorer_error`      | `status == "completed"` and `scoring_details.error` non-empty|
| `low_recall`        | `scoring_details.recall < LOW_RECALL_THRESHOLD` (0.5)        |
| `backend_overlap`   | bias warning: config's MCP surface overlaps GT backend       |
| `overshipping`      | bias warning: capability-boundary anti-pattern               |

Bias-warning kinds with a `task_id` or `affected_tasks` in their `detail`
are fanned out per-trial. Experiment-level kinds
(`no_independent_baseline`) appear in `overall.experiment_warnings`
instead.

`low_quality_trials` lists every row whose validity is `invalid` *or*
whose `quality_flags` is non-empty.

## Adapter pattern

`TraceQualityReporter` exposes two builder paths:

```python
# codeprobe-native: consumes CompletedTask + BiasWarning records
TraceQualityReporter.from_completed_tasks(
    config_results: Mapping[str, Sequence[CompletedTask]],
    bias_warnings: Sequence[BiasWarning] = (),
)

# Adapter entry point: consume pre-built rows from any benchmark
TraceQualityReporter.from_metrics(
    metrics: Sequence[TraceQualityMetrics],
    experiment_warnings: Sequence[str] = (),
)
```

Adapters in other benchmarks build `TraceQualityMetrics` rows from their
own result formats and call `from_metrics` to inherit the summary,
low-quality iteration, and JSON serialization without depending on
codeprobe's `CompletedTask` shape.

### EnterpriseBench adapter sketch

`results/<run>/results.json` carries one record per task with:

```jsonc
{
  "task_id": "...",
  "success": true,
  "phase": "complete",        // mapped → validity
  "failure_class": null,      // mapped → validity_reason / flag
  "scores": {
    "task_score": 0.0,
    "all_passed": false,
    "checkpoints_passed": 0,
    "checkpoints_total": 3,
    "checkpoints": [{ "name": "...", "passed": false, ... }]
  }
}
```

EB-specific mapping (to be implemented in the EB rig):

* `phase != "complete"` or `success == false && failure_class != null`
  → `validity = "invalid"`, `validity_reason = failure_class`.
* Per-checkpoint failures → `quality_flags` (e.g.
  `checkpoint_drift_points_failed`) so checkpoint-level signal is
  preserved per trial.
* `precision = checkpoints_passed / checkpoints_total` when
  `checkpoints_total > 0` — surfaces an oracle metric without inventing
  a score.

The follow-up bead in the EnterpriseBench rig owns the actual adapter
implementation; this module only owns the abstraction.

### CodeScaleBench adapter sketch

`scripts/csb_metrics/trace_quality.py` already has a richer per-stage
view (`stage1_class`, `stage2_class`, hallucination, retrieval). When CSB
resumes the existing `TraceQualityMetrics` rows can be flattened into
codeprobe's row shape with:

* `stage1_class == "invalid"` → `validity = "invalid"`,
  `validity_reason = stage1_reason`.
* `stage2_class == "valid_badsetup"` → flag `bad_setup`.
* `hallucination_detected` → flag `hallucination` (+ `precision`,
  `recall` from the hallucination block).
* `retrieval.file_recall < 0.5` → flag `low_retrieval_recall`.

CSB enablement is explicitly out of scope for the codeprobe-bygh bead.

## ZFC compliance

Every signal in this module is a structural read from a typed field —
no semantic judgment, no keyword matching, no model calls. The single
threshold (`LOW_RECALL_THRESHOLD = 0.5`) surfaces an existing oracle
metric and is constructor-overridable per experiment.

The reporter is documented as ZFC-compliant in
`CLAUDE.md` alongside `analysis/stats.py` (deterministic arithmetic) and
`analysis/ranking.py` (deterministic ranking with explicit tiebreakers).

## Stability

`schema_version = 1`. Bumps required for:

* Removing or renaming any field on `TraceQualityMetrics` /
  `TraceQualitySummary`.
* Changing the validity classes (`valid` / `invalid`) or removing a
  flag-name → source mapping.
* Changing the meaning of `low_quality_trials`.

Adding a new flag kind or summary field is **non-breaking** — downstream
consumers should ignore unknown flag names rather than fail closed.
