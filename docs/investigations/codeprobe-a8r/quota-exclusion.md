# codeprobe-a8r — Quota-errored trials contaminate published mean_score

**Status:** branch-ready (commit `63fbb5b` on `codeprobe-a8r-quota-exclude`), awaiting Stephanie merge sign-off.
**Source:** DEEP_AUDIT 2026-06-15, finding CRITICAL #1 (`.gc-reports/audit-2026-06-15.md`).

## Problem

Quota-errored trials are stamped `automated_score=0.0` + `error_category="quota"`
by the executor (`core/executor.py`) as an unrecoverable infrastructure failure.
Both stats summarizers built `scores = [t.automated_score for t in tasks]` over
*all* tasks, rolling that 0.0 into the headline `mean_score`, `median_score`,
pass-rate, and CIs — directly contradicting the `quota_error_count` docstring
contract (`stats.py`, codeprobe-9xrl) which states quota errors "should NOT roll
into `mean_score`." Any run with quota casualties was biased toward zero, which
can shift a published `compare_configs` winner.

## Fix

- `analysis/stats.py`: new `is_quota_casualty(task)` single-source predicate
  (`error_category == "quota"`). Both `summarize_config` and
  `summarize_completed_tasks` exclude quota trials from the reward population
  (`scores` / `durations` / pass-rate) while keeping them in the structural
  totals (`total_tasks` / `completed` / `errored`) and surfacing them via
  `quota_error_count`. All-quota edge case guarded against
  ZeroDivision/StatisticsError (zeroed stats, count still surfaced).
- `analysis/report.py`: `generate_report` and `_tee_task_scores` omit quota
  trials from the paired score sink so `compare_configs` hypothesis tests
  operate on the same reward population the summaries report.
- `tests/test_stats.py::TestQuotaExclusion`: K-quota + M-real mix asserts
  mean/median/pass-rate over real trials only (A1), `quota_error_count == K`
  (A2), both summarizers agree, all-quota edge case, no-quota backwards-compat,
  and paired-sink exclusion.

## Acceptance

- A1 — mean over the M real trials only: PASS.
- A2 — `quota_error_count == K` surfaced: PASS.
- A3 — fixture tests added & green, existing stats/analysis/report suites pass: PASS.
- A4 — no new hardcoded thresholds (verifier-honesty lint green): PASS.

## Verification

```
PYTHONPATH=<worktree>/src python -m pytest \
  tests/test_stats.py tests/test_analysis.py tests/test_report_dual.py \
  tests/test_dual_matrix.py tests/lint/test_scorer_honesty.py tests/analysis -q
# 186 passed
```

(Editable install is pinned to the main repo `src/`; the worktree must be put
on `PYTHONPATH` to exercise the branch's code.)

Independent reviewer (opus code-reviewer, role-clamped, actively tested):
verdict **PASS** — mean over real trials confirmed (0.4 vs contaminated 0.229),
both summarizers byte-identical via `dataclasses.asdict`, paired sink omits
quota, all-quota path crash-free.

## Out-of-scope follow-up (filed separately)

The reviewer found the same contamination in three executor/CLI published-mean
paths *not* named in this bead's scope (which covers only the `analysis/`
summarizers + the compare path):

- `core/experiment.py` `_compute_summary` → `mean_automated_score` in results.json
- `cli/experiment_cmd.py` headline-reward `scores` build (~:473 / :554)
- `cli/run_cmd.py` terminal-summary `scores` build (~:917 / :994)

`is_quota_casualty()` now exists as the reusable predicate to fix them.
