# Infra-failure validity triage

> Fetch-on-demand playbook for the infra-failure triage gate (codeprobe-77z).
> The authoritative code is `src/codeprobe/analysis/validity.py`.

## Why

A trial that crashed on infrastructure — an output-token-ceiling overrun,
quota/OAuth exhaustion, a rate limit, a network/timeout, an MCP connect
failure, or a process crash — is stamped `automated_score=0.0` by the executor.
That `0.0` is **not** a measurement of solution quality. Rolling it into the
reward mean biases a comparison toward zero, and a run that still holds
unresolved infra casualties is not safely quotable.

The motivating regression is the codeprobe-9tk flagship confirm run: a
`with-sg-full` trial (0d4ec3ad) overran the 32000-output-token CLI ceiling, was
recorded `status=error` with empty `scoring_details` and `automated_score=0.0`,
and dragged that arm's mean 0.72 → 0.645. Stephanie's directive (Slack
C0B1A0CKEH0, 2026-06-14): "rerun any infra failure ones, that should fail our
validity triage gates."

This generalizes two narrower exclusions:

- **codeprobe-a8r / codeprobe-9jxx** — `is_quota_casualty`, keyed on
  `error_category == "quota"`.
- **codeprobe-h3j4** — `is_scorable_run`, which drops every `status == "error"`
  row from the reward population.

`is_infra_failure` is a strict superset of `is_quota_casualty`, and it closes
the hole the status filter alone leaves open: a crash recorded as a terminal
`failed` row rather than an `error` row used to keep its 0.0 in the mean.

## ZFC boundary

The classifier reads **structural and string signals only** — the trial
`status`, whether `scoring_details` ran end-to-end, the adapter-declared
`result_subtype`, the executor's `error_category`, and infra fault markers in
the recorded error text. It applies **no score threshold**: a genuinely low
score is a real data point and is never reclassified as infra. This is the same
shape as the executor's own `_classify_error` — string-signal fault
classification, not semantic scoring. The marker list is a mechanical
fault-signature table, like a quota-pattern matcher.

## The three trial classes

`classify_trial(task) -> TrialClass`, in precedence order:

| # | Signal | Class | Reward population? | Gate |
|---|--------|-------|--------------------|------|
| 1 | `error_category` in `{quota, timeout, system}` | `INFRA_FAILURE` | **excluded** | **fail until re-run** |
| 2 | `status == "completed"` or non-empty `scoring_details` | `VALID` | **in** | pass |
| 3 | infra marker in the error text (token-ceiling, API Error, rate limit, OAuth, usage/session limit, budget exceeded, halting, connection refused/reset/failed, network unreachable, failed to connect, timed out) | `INFRA_FAILURE` | **excluded** | **fail until re-run** |
| 4 | terminal `result_subtype` (`error_max_turns`), or `status == "failed"` | `GENUINE_FAILURE` | **in** | pass |
| 5 | remaining `status == "error"` | `INFRA_FAILURE` | **excluded** | **fail until re-run** |

Two ordering decisions carry the weight:

- **`error_category` outranks everything (1 before 2).** The executor stamps an
  `error_category` only on rows that never produced a measurement — the scored
  path builds its `CompletedTask` without one at all. Ranking it first is what
  keeps `is_infra_failure` a strict superset of `is_quota_casualty`, including
  the executor-stamped quota rows whose `status` still reads `completed`.
- **Text markers rank BELOW the VALID check (3 after 2) and ABOVE the terminal
  subtype (3 before 4).** Below VALID, so stray fault vocabulary in the metadata
  of a trial that *did* score cannot throw a real measurement away. Above the
  subtype, mirroring the adapter's own precedence — an infra stub is a casualty
  even when the CLI also reported `error_max_turns` (see `adapters/claude.py`,
  "Quota wins over subtype").

`error_max_turns` is deliberately kept distinct from infra so cap-contamination
(codeprobe-8up) is never conflated with a crash: the agent DID get its turns, so
re-running it would only burn budget hitting the same cap.

The markers are **word-anchored** regexes. "the rate limiter tests failed" and
"OpenAPI errors" are NOT infra — an unanchored substring match would have pulled
those genuine terminal failures out of the reward population.

## The three surfaces

1. **Predicate** — `is_infra_failure(task)`, composed into
   `stats.is_scorable_run`, is the single source of truth for the reward
   population. Both `summarize_config` / `summarize_completed_tasks` and the
   paired hypothesis tests in `compare_configs` exclude infra casualties from
   `mean_score` / `median` / `pass_rate` / CIs, while keeping them in the
   structural totals (`total_tasks` / `errored` / cost / tokens) and surfacing
   the count via `ConfigSummary.infra_failure_count` (`quota_error_count`
   remains the quota-specific sub-count for the codeprobe-9xrl contract).

2. **Gate** — `triage_run(tasks) -> ValidityReport`. `passed` is `True` only at
   zero infra failures; otherwise it FAILs and lists the offending trial ids
   (`<task_id>#rep<repeat_index>`). Both `generate_report` and
   `generate_report_streaming` attach it as `Report.validity` (the streaming
   path accumulates it through `ValidityTriage` so no trial is buffered).

3. **Render** — `format_text_report` prints a `### Validity` verdict (and a
   per-arm `⚠ N infra failure(s)` suffix beside the mean); `format_json_report`
   emits a `validity` object; `codeprobe interpret` lifts `validity` to a
   top-level envelope field. Exclusions are **never silent** (adapter-contract
   honesty).

## Run-closer contract

The run-closer / writeup step **must** check the gate and block
"quotable/complete" status while it FAILs:

```python
from codeprobe.analysis import generate_report

report = generate_report(name, all_results, configs=configs)
if report.validity is not None and not report.validity.passed:
    # NOT quotable — re-run the infra trials to 'completed' (or reclassify
    # them genuine with a reason) before publishing any mean/ranking/comparison.
    raise SystemExit(report.validity.summary())
```

Equivalently, gate on the `interpret` envelope's top-level `validity.passed`.

## Adding an infra signature

New CLI/runtime fault markers go in `_INFRA_TEXT_PATTERNS` (word-anchored,
case-insensitive regexes) or `_INFRA_ERROR_CATEGORIES` (executor
`error_category` values) in `validity.py`, plus a regression case in
`tests/analysis/test_validity.py::TestClassifyTrial::test_infra_text_signatures`
and a false-positive guard in `test_infra_markers_do_not_fire_on_unrelated_text`.
Keep markers mechanical — they are fault signatures, not quality heuristics.
