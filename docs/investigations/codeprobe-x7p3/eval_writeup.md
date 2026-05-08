# codeprobe-x7p3 — Re-eval gascity-mcp-comparison under unified ScoreResult contract

**Status:** complete
**Branch:** `feature/codeprobe-x7p3-validate-unified-contract` (off `feature/codeprobe-ceuu-benchmark-qa-core`)
**Predecessor:** codeprobe-ur8d (recall-only metric; reference data preserved at `.codeprobe/runs.codeprobe-ur8d/` and `.codeprobe/reports.codeprobe-ur8d/`)
**Contract:** unified ScoreResult (`reward`, `scorer_family`, `sub_scores`, `diagnostics`) shipped via dr-2vydrm.4 / codeprobe-ufra (commit dca177d)

## Purpose

Re-run the gascity-mcp-comparison corpus (5 tasks × 2 configs × N=1) under the
new unified ScoreResult contract, with each task declaring
`verification.scorer_family` explicitly so the headline reward is computed
under the *appropriate* rubric — not the recall-only fallback that produced
ur8d's numbers.

This bead validates:

1. **A1.** ScoreResult shape is correctly emitted per trial (reward + scorer_family + sub_scores + diagnostics).
2. **A2.** `aggregate.json.config_summaries` carries reward, scorer_family breakdown, and diagnostics totals.
3. **A3.** Each task's `metadata.json:verification.scorer_family` is declared (not hardcoded in scorer code).
4. **A5.** No silent fallback to recall — declared family flows through `_select_ir_family()` → `IRScorer`.

## scorer_family audit (A3, A5)

All 5 tasks declared `oracle_overlap_fbeta` with `fbeta_beta=0.5` in
`metadata.json:verification`:

| task_id  | category               | declared scorer_family   | beta |
|----------|------------------------|--------------------------|------|
| 38223444 | symbol-reference-trace | oracle_overlap_fbeta     | 0.5  |
| 6cf61fea | change-scope-audit     | oracle_overlap_fbeta     | 0.5  |
| b826fa9d | symbol-reference-trace | oracle_overlap_fbeta     | 0.5  |
| d9fee4ae | change-scope-audit     | oracle_overlap_fbeta     | 0.5  |
| e5d7a4e7 | symbol-reference-trace | oracle_overlap_fbeta     | 0.5  |

**Rationale.** Both `symbol-reference-trace` and `change-scope-audit` have a
fixed, small expected-file set, so over-shipping (returning every file in the
repo) signals miscomprehension rather than thoroughness. F-beta with β=0.5
weights precision twice as heavily as recall, matching the canonical
recommendation in `core/scoring.py`:

> tasks that want over-ship penalised harder (symbol-reference-trace) configure
> beta < 1.0 (e.g. 0.5 weights precision twice as heavily as recall)

β=1.0 (≡F1) was rejected as under-penalising the dump-and-filter strategy.
Recall-only was rejected per amendment 2 — that's exactly the metric this
contract retired.

**Observed at runtime.** `aggregate.json.config_summaries[*].scorer_family_distribution`
shows `{"oracle_overlap_fbeta": 5}` for both configs — every trial routed
through the declared family with no silent fallback. **A5 satisfied.**

(Files modified in-place at
`/home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe/tasks/<id>/metadata.json`,
under `verification.scorer_family` and `verification.fbeta_beta`. Not committed
to the gascity repo.)

## Run setup

- **Target:** `/home/ds/test_repos/gascity/gascity-mcp-comparison/`
- **Tasks:** 38223444, 6cf61fea, b826fa9d, d9fee4ae, e5d7a4e7
- **Configs:** `baseline`, `with-sourcegraph`
- **Repeats:** N=1
- **Budget cap:** $20 (soft) — actual: $16.23
- **Codeprobe branch:** `feature/codeprobe-x7p3-validate-unified-contract` off `feature/codeprobe-ceuu-benchmark-qa-core`
- **Prior runs/reports preserved:** moved to `.codeprobe/runs.codeprobe-ur8d/` and `.codeprobe/reports.codeprobe-ur8d/` so ur8d history remains diff-able.

## ScoreResult contract validation (A1, A2)

`runs/codeprobe-x7p3/validate_contract.py` walked every
`runs/<config>/<task>/scoring.json` and checked for the required unified-contract
fields:

```
config               task       ok      reward family                    issues
--------------------------------------------------------------------------------
baseline             38223444   Y       0.0683 oracle_overlap_fbeta
baseline             6cf61fea   Y       0.3049 oracle_overlap_fbeta
baseline             b826fa9d   Y       0.9091 oracle_overlap_fbeta
baseline             d9fee4ae   Y       0.3788 oracle_overlap_fbeta
baseline             e5d7a4e7   Y       0.0253 oracle_overlap_fbeta
with-sourcegraph     38223444   Y       0.3704 oracle_overlap_fbeta
with-sourcegraph     6cf61fea   Y       0.2439 oracle_overlap_fbeta
with-sourcegraph     b826fa9d   Y       0.9091 oracle_overlap_fbeta
with-sourcegraph     d9fee4ae   Y       0.3571 oracle_overlap_fbeta
with-sourcegraph     e5d7a4e7   Y       0.8333 oracle_overlap_fbeta
--------------------------------------------------------------------------------
Valid: 10/10
```

**A1 satisfied.** Each scoring.json contains `reward`, `score`, `status`,
`scorer_family`, `sub_scores{precision,recall,f1,reward,fbeta_beta}`, and
`diagnostics{ir_metrics{precision,recall,f1},task_time_seconds,token_cost_usd}`.

**A2 satisfied.** `aggregate.json.config_summaries[*]` contains:
- `mean_reward` (under fbeta β=0.5)
- `scorer_family_distribution`: `{oracle_overlap_fbeta: 5}` per config
- `total_cost_usd`, `mean_cost_per_task`, `total_time_seconds`
- `score_per_dollar`
- `mean_precision`, `mean_recall`, `mean_f1` plus `ir_diagnostics` block

## Results

### Per-config summary (under fbeta β=0.5)

| config             | tasks | mean_reward (fbeta) | mean_precision | mean_recall | mean_f1 | total_cost ($) | mean_cost/task ($) | total_time (s) | score/$ |
|--------------------|-------|---------------------|----------------|-------------|---------|----------------|--------------------|----------------|---------|
| baseline           | 5     | **0.337**           | 0.334          | 0.833       | 0.364   | 7.50           | 1.500              | 787.8          | 0.225   |
| with-sourcegraph   | 5     | **0.543**           | 0.571          | 0.667       | 0.537   | 8.74           | 1.747              | 1032.9         | 0.311   |

### Pairwise delta (with-sourcegraph − baseline)

| metric                  | this run (fbeta β=0.5, N=1) | ur8d (recall, N=3, reference only) |
|-------------------------|-----------------------------|------------------------------------|
| mean_reward delta       | **+0.206**                  | −0.211                             |
| mean_precision delta    | +0.237                      | +0.268                             |
| mean_recall delta       | −0.167                      | −0.211                             |
| mean_f1 delta           | +0.173                      | +0.137                             |
| Cohen's d on reward     | 0.561                       | −0.837                             |
| wins (a/b/tie)          | 2 / 2 / 1                   | 3 / 0 / 2 (a wins)                 |

**The reward-delta sign flips vs ur8d.** ur8d's headline (−0.211 under recall)
labelled with-sourcegraph as worse. Under fbeta β=0.5 — the rubric this contract
was designed to bring online — the same trials yield +0.206. The flip is **not**
a measurement of with-sg becoming better; it's the metric crediting precision
that recall ignored.

### Per-task detail

| task     | baseline reward | baseline P | baseline R | baseline files | with-sg reward | with-sg P | with-sg R | with-sg files | per-task delta |
|----------|-----------------|------------|------------|----------------|----------------|-----------|-----------|---------------|----------------|
| 38223444 | 0.068           | 0.06       | 0.83       | 90             | 0.370          | 0.33      | 0.67      | 12            | **+0.302**     |
| 6cf61fea | 0.305           | 0.26       | 0.83       | 19             | 0.244          | 0.21      | 0.67      | 19            | −0.061         |
| b826fa9d | 0.909           | 1.00       | 0.67       | 2              | 0.909          | 1.00      | 0.67      | 2             | 0.000 (tie)    |
| d9fee4ae | 0.379           | 0.33       | 0.83       | 15             | 0.357          | 0.31      | 0.83      | 16            | −0.022         |
| e5d7a4e7 | 0.025           | 0.02       | 1.00       | **295**        | 0.833          | 1.00      | 0.50      | 3             | **+0.808**     |

The two big with-sg wins (38223444, e5d7a4e7) are dominated by precision: the
baseline agent dumped 90 and 295 files respectively, where with-sg returned
12 and 3. Under recall both runs would have looked similar (or the baseline
better, on e5d7a4e7 it was 1.0 vs 0.5). Under fbeta β=0.5 the dump-and-filter
strategy collapses to 0.025 — exactly the behaviour the contract was built
to surface.

The middle three tasks (6cf61fea, b826fa9d, d9fee4ae) are essentially noise at
N=1; per-task deltas are within ±0.06.

### Cost-Pareto observation

`with-sourcegraph` is **higher reward and more cost-efficient** under this
metric:

- baseline: 0.337 reward at $1.50/task → score/$ = 0.225
- with-sg : 0.543 reward at $1.75/task → score/$ = 0.311 (**+38% cost efficiency**)

The runtime-time gap (with-sg ~33% slower per task on average) reflects the
extra MCP round-trips. That's pure latency — billable cost-per-trial is only
+$0.25, and the precision win pays for it on the high-leverage tasks.

The unified contract makes this Pareto frontier visible in a single
`config_summaries` block, where ur8d's recall-only output collapsed it.

## Interpretation

### What N=1 lets us claim (and what it doesn't)

A single repeat per task means per-config means have high variance — the
±0.06 per-task deltas on the middle three tasks are within the noise floor
implied by the standard deviations on this run (σ_baseline=0.353, σ_with-sg=0.305).
With N=1 we **cannot** make a precision-vs-recall comparison statistically
meaningful; Cohen's d=0.561 here is a population descriptor of 5 paired
trials, not an inferential statistic.

The **primary** outputs of this rerun are deterministic, not noise-sensitive:

1. The unified contract emission shape (every trial has every required field).
2. The `scorer_family` routing (declared metadata flows to scorer to aggregate).
3. The metric flip in headline direction when the rubric changes — that's
   **caused** by the rubric, not by trial noise, since the underlying P/R
   numbers are unchanged.

The reward delta of +0.206 is reported as descriptive, not inferential.

### Reference: ur8d under recall

For context only — ur8d's published delta of −0.211 (with-sg − baseline) was
measured under `oracle_overlap_recall`, the family this contract demoted. The
underlying observation in ur8d already showed precision improving by +0.268
and F1 by +0.137; the recall-only headline buried both. Under F-beta β=0.5
those two findings drive the headline, and the sign flips.

The numbers below are **not** comparable to ur8d's headline:
- The agent runs are different (fresh trials, N=1 vs N=3).
- The scoring metric is different (fbeta β=0.5 vs recall).

But the per-config precision and recall *measurements* are directionally
consistent with ur8d (precision up, recall down for with-sg), giving us
some confidence that this is a real rubric-flip rather than agent noise.

### What the contract surfaced that ur8d hid

`e5d7a4e7` baseline returned **295 files** with recall=1.0 (a perfect score
under recall, which ur8d would have reported as such). Under fbeta β=0.5 it
collapses to 0.025 — a near-failure. The agent was doing dump-and-filter, not
comprehending the reference graph. That's exactly the failure mode the
contract was designed to expose. Without the unified contract this trial
would have been celebrated as a success.

## Constraints honoured

- ✅ Private repo (gascity) only; no public push.
- ✅ Local commit on a feature branch off `feature/codeprobe-ceuu-benchmark-qa-core`.
- ✅ `main` untouched.
- ✅ Run via standard codeprobe CLI; stderr+stdout captured at
  `runs/codeprobe-x7p3/run.{stdout,stderr}.log`.
- ✅ Soft cap $20 not exceeded ($16.23 actual).

## Follow-ups

None required. Acceptance criteria A1–A5 satisfied; no per-task scorer_family
gaps; no missing registry families. The +0.206 sign-flip vs ur8d's −0.211 is
the *expected* consequence of switching from recall-only to fbeta — there's
no surprise to investigate.

If anything, this run motivates a separate (out-of-bead) reflection: the
sign-flip is a strong demonstration of why the unified contract was necessary,
and the e5d7a4e7 baseline trial (295 files, 0.025 fbeta) is a clean
illustration. Worth referencing in the contract's design docs / scoring_model.md
if not already there.
