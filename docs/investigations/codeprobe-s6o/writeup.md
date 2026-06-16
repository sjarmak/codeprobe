# codeprobe-s6o — Sequential execution drops whole config on uncaught scorer exception

Branch: `codeprobe-s6o-seq-exec-envelope` (worktree `/home/ds/projects/codeprobe-s6o`, off `main`).
P1, DEEP_AUDIT 2026-06-15 CRITICAL #2 + HIGH #3. HALT at branch-ready — not pushed/merged.

## The bug (CRITICAL #2)

`execute_config`'s sequential path (the DEFAULT, `parallel=1`) called
`_run_one → execute_task` with **no exception envelope**, while the parallel
path wrapped `future.result()` in `except Exception → status="error"`. An
uncaught scorer exception (a KeyError, a scorer bug — `execute_task` only
catches OSError/JSONDecodeError/ValueError/TypeError) propagated out of
`execute_config` and **dropped every already-collected result for that
config**. Violated CLAUDE.md: "Don't drop score failures → score them as
'incorrect' rather than dropping."

## Fix

- `executor.py` — factor the crash envelope both paths build into a shared
  `_crash_result(task_dir, repeat_index, exc)` closure; guard the sequential
  `_run_one` call with `except Exception → _crash_result(...)`. The parallel
  path now calls the same helper, so the two paths are identical by
  construction (A2).
- HIGH #3 enabler — decompose the ~544-line `execute_task`: extract the
  scoring stage into `_score_in_sandbox(...)` (snapshot → stage answers →
  score → project) and `_build_scoring_details(score_result)`. `execute_task`
  drops to ~437 lines; the scoring stage is now a named, independently-testable
  unit.

## Commits

- `70f16bd` — fix(executor): preserve per-task crash in sequential path.
- `66a5cff` — refactor(executor): extract scoring stage from execute_task.

## Acceptance

- **A1** ✓ Sequential run with a raising scorer completes; every trial is a
  preserved `status="error"` result (scored 0.0, not dropped); sibling trials
  retained. (`test_execute_config_sequential_preserves_scorer_crash`)
- **A2** ✓ The parallel path preserves the same crash identically — both route
  through `_crash_result`. (`test_execute_config_parallel_preserves_scorer_crash`)
- **A3** ✓ Scoring stage extracted to named helpers; `_build_scoring_details`
  is 23 lines with a direct unit test. `_score_in_sandbox` is ~129: the
  `TemporaryDirectory` context binds staging+scoring, so splitting further
  would fragment the temp lifecycle ("<50 where practical").
- **A4** ✓ New tests green; full suite **3790 passed**, 7 skipped, 1 xfailed
  (release-gate real-wheel build + load-flaky `mine_parallel` SQLite test
  excluded — both pre-existing, unrelated to this diff).

## Notes

- No observable-score change on a clean run — this only changes drop→preserve
  behaviour on a crash.
