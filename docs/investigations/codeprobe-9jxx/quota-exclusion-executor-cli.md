# codeprobe-9jxx — Quota casualties contaminate mean_automated_score in executor + CLI published-mean paths

**Status:** branch-ready on `codeprobe-9jxx-quota-exec`, awaiting Stephanie merge sign-off (CHANGES OBSERVABLE SCORES — halt-at-branch-ready per the bead).
**Source:** Follow-up to codeprobe-a8r (DEEP_AUDIT 2026-06-15 CRITICAL #1). The opus code-reviewer found the same quota contamination in three executor/CLI published-mean paths that a8r's stated scope did not cover.

## Problem

Quota-errored trials are stamped `automated_score=0.0` + `error_category="quota"`
by the executor (`core/executor.py`) as an unrecoverable infrastructure failure.
a8r fixed the `analysis/stats.py` summarizers and the `compare_configs` paired
path via the reusable predicate `is_quota_casualty(task)`, but three other paths
that publish a mean rolled that 0.0 into their reward population with no
exclusion:

- `core/experiment.py:_compute_summary` — `score_sum += t.automated_score; n += 1; mean = score_sum / n` over all completed tasks. Writes `mean_automated_score` to `results.json` (consumed by `cli/run_cmd.py` and `api.py`).
- `cli/experiment_cmd.py:experiment_aggregate` — `scores = [r["automated_score"] for r in cfg_rows]` feeding the headline `mean_automated_score` + `stdev`.
- `cli/run_cmd.py` — two terminal-summary score builds (the pretty per-config line and the envelope/NDJSON summary).

Any run with quota casualties was biased toward zero, which can shift a published
`mean_automated_score` and the reward population behind a `compare_configs` winner.

## Fix

- `analysis/stats.py`: new `partition_reward_population(tasks) -> (reward_tasks, quota_count)` helper next to the SSOT `is_quota_casualty` predicate. It is the single mechanical split (real trials vs quota count) that the published-mean paths route through, so the `[t for t in tasks if not is_quota_casualty(t)]` idiom (previously inlined in four places) lives in one tested place. `summarize_config` was refactored onto it (behavior identical — `total - scored_total` is algebraically `len(tasks) - len(reward_tasks)`).
- `core/experiment.py:_compute_summary` (B1): excludes quota casualties from `score_sum`/`reward_n`, the duration total, and oracle metrics; `mean_automated_score` is over real trials only. Cost/token totals stay over all completed trials (the quota attempts still cost real money). `quota_error_count` surfaced; `score_per_dollar` divides by `reward_n` and is guarded against an empty reward population. This loop keeps an inline single-pass quota check (it interleaves cost/token accumulation over all trials with score accumulation over the reward set), so it deliberately does not call the partition helper.
- `cli/experiment_cmd.py:experiment_aggregate` (B2): headline `mean_automated_score`, `mean_reward`, and `stdev_automated_score` are built from the reward population via `partition_reward_population`; `quota_error_count` added to `config_summaries`. `tasks_completed` and cost/time/token totals stay over all rows.
- `cli/run_cmd.py` (B3): both terminal-summary paths route through the reward population. The envelope/NDJSON build was extracted to a pure, unit-tested `build_run_envelope_summary(results_by_config)`; the pretty per-config line excludes quota from its mean and pass-rate and shows a `(N quota-excluded)` note. `tasks`/`total_tasks`/`cost_usd` stay over all attempts.

## Tests (B5)

- `tests/test_stats.py::TestQuotaExclusion` — direct coverage of `partition_reward_population` (splits + counts, no-quota, all-quota).
- `tests/test_experiment_core.py` — `_compute_summary` excludes the 0.0 quota stub from `mean_automated_score`; surfaces `quota_error_count`; keeps cost over all attempts; all-quota yields mean 0.0 with no `score_per_dollar` and no crash.
- `tests/test_experiment_cmd.py` — aggregate headline mean + stdev exclude quota; `quota_error_count` surfaced; `tasks_completed`/cost unchanged.
- `tests/test_run_envelope_summary.py` — envelope mean/perfect over reward population, structural totals + quota count preserved, no-quota regression guard.
- `tests/cli/test_no_bare_usage_errors.py` — whitelist line numbers resynced (+1) for the new `analysis.stats` import in `experiment_cmd.py`.

Full suite: `3894 passed, 7 skipped, 1 xfailed` (run with `PYTHONPATH=src`; the
editable install points at a different checkout). The one deselected failure,
`tests/test_release_gate.py::test_build_and_stage_real_wheel`, is a pre-existing
environment failure (isolated-venv wheel build) — confirmed identical on the
pristine `main` checkout, unrelated to this change. `ruff check` clean on all
touched files (≤120 cols).

## Acceptance

- B1 — `_compute_summary` excludes quota from the score mean: PASS.
- B2 — aggregate headline mean + stdev exclude quota: PASS.
- B3 — both run_cmd terminal-summary score builds exclude quota: PASS.
- B4 — quota counts surfaced; structural totals (counts, cost) unchanged: PASS.
- B5 — tests assert exclusion per path; existing tests + verifier-honesty lint green: PASS.
- B6 — no new hardcoded thresholds (the only float literal is the pre-existing `score >= 1.0` "perfect" definition): PASS.

Verified by two independent verification agents (code-reviewer + python-reviewer),
both PASS/APPROVE on all six criteria.

## Known latent issue (out of scope — tracked, not fixed here)

In `cli/experiment_cmd.py:experiment_aggregate`, the oracle-metric `_detail_values`
helper and the `family_counts` distribution still iterate over all `cfg_rows`
(the row dicts do not carry `error_category`, so they cannot be filtered there
without threading the field through). Today this is harmless: quota casualties
carry empty `scoring_details`, so they contribute nothing to precision/recall/f1,
and they land in the `family_counts["unspecified"]` bucket — a minor inflation of
that one counter, not the headline mean. This is pre-existing behavior that this
change did not introduce or worsen, and it is outside the bead's B1–B6 scope
(headline mean/stdev). Flagged by the python-reviewer as a latent trap should a
future executor stamp quota tasks with non-empty `scoring_details`. Candidate for
a follow-up bead if the family distribution's quota handling needs to be exact.
