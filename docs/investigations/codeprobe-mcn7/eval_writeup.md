# codeprobe-mcn7 — SDLC family rerun at N=3

**Status:** complete
**Branch:** `feature/codeprobe-x7p3-validate-unified-contract`
**Predecessor:** codeprobe-3oms (closed) — single-trial mixed-family MCP comparison

## Purpose

codeprobe-3oms reported a per-family delta of **+0.054** on the SDLC
(`continuous`) family with Sourcegraph MCP enabled, but at N=1 the per-task
variance dominated. This rerun bounds the delta with N=3 paired repeats and
specifically tests whether the +0.300 win on `0d4ec3ad` reproduces.

Per-task results from 3oms (N=1) for reference:

| task     | b_rew  | w_rew  | delta   |
|----------|--------|--------|---------|
| 0d4ec3ad | 0.500  | 0.800  | +0.300  |
| 45b581b5 | 0.800  | 0.800  | 0.000   |
| ba1f3675 | 0.582  | 0.543  | −0.038  |
| d906ac3d | 0.603  | 0.610  | +0.007  |
| fde8e6e0 | 0.678  | 0.682  | +0.003  |

## TL;DR

The 3oms +0.054 family delta does **not** reproduce at N=3. The mean per-task
delta tightens to **+0.0035** with 95% CI **[−0.0005, +0.0074]** — the CI
just barely includes zero, and the paired-t statistic (t=2.41, df=4) is
below the two-sided t-critical of 2.776, so the family-level effect is
**not significant at α=0.05**.

The +0.300 win on 0d4ec3ad **does not reproduce** at all: both configs
hit exactly 0.800 across all 3 repeats (Δ=0.000). The 3oms outlier was a
single-trial fluke driven by a baseline 0.500 score that disappeared once
the trial was repeated.

## Run setup

- **Target:** `/home/ds/test_repos/gascity/gascity-mcp-comparison/`
- **Tasks:** 5 SDLC (`continuous`) tasks: ba1f3675, d906ac3d, 0d4ec3ad, 45b581b5, fde8e6e0
- **Configs:** `baseline`, `with-sourcegraph` (Sourcegraph HTTP MCP)
- **Repeats:** N=3 per task per config (30 trials)
- **Model:** `claude-sonnet-4-6`
- **Concurrency:** `--parallel 2` (reduced from default 5 to throttle Claude rate-limit burst)
- **Soft cap:** `--max-cost-usd 80` per config (with-sg overran by ~$5)
- **Suite filter:** `docs/investigations/codeprobe-mcn7/suite-sdlc-only.toml`
- **Prior runs preserved:** `runs.codeprobe-3oms/`, `runs.codeprobe-mcn7-failed/` (rate-limited attempts)

### Run history

- **attempt-1** (logs.attempt-1/): partial run, hit Claude session rate limit mid-execution
- **attempt-2** (logs.attempt-2/): all 30 trials hit "You've hit your limit · resets 12:40pm" — every scoring.json had status=error, reward=0.0
- **attempt-3** (logs/): clean run after 12:40 ET reset, all 30 trials completed successfully

The attempt-1/attempt-2 directories are kept for forensic context — they
explain the cost overrun (~$11.60 was burned on cache reads during the
rate-limited sessions before the limit reset).

## Aggregate results

### Per-task means and stds (N=3)

| task     | baseline mean ± std | rewards [r0,r1,r2]            | with-sg mean ± std  | rewards [r0,r1,r2]            | Δ (paired) |
|----------|---------------------|-------------------------------|---------------------|-------------------------------|------------|
| ba1f3675 | 0.5340 ± 0.0000     | [0.5340, 0.5340, 0.5340]      | 0.5403 ± 0.0025     | [0.5388, 0.5432, 0.5388]      | +0.0063    |
| d906ac3d | 0.6031 ± 0.0000     | [0.6031, 0.6031, 0.6031]      | 0.6079 ± 0.0000     | [0.6079, 0.6079, 0.6079]      | +0.0048    |
| 0d4ec3ad | 0.8000 ± 0.0000     | [0.8000, 0.8000, 0.8000]      | 0.8000 ± 0.0000     | [0.8000, 0.8000, 0.8000]      | 0.0000     |
| 45b581b5 | 0.8000 ± 0.0000     | [0.8000, 0.8000, 0.8000]      | 0.8000 ± 0.0000     | [0.8000, 0.8000, 0.8000]      | 0.0000     |
| fde8e6e0 | 0.6784 ± 0.0000     | [0.6784, 0.6784, 0.6784]      | 0.6846 ± 0.0000     | [0.6846, 0.6846, 0.6846]      | +0.0062    |

### Family-level paired delta

- **Mean per-task delta** (with-sg − baseline): **+0.00345**
- **Stdev across 5 per-task deltas:** 0.00321
- **Standard error:** 0.00143
- **95% CI** (paired-t, df=4, t-crit=2.776): **[−0.00053, +0.00743]**
- **t-statistic:** +2.408, df=4 (below t-crit=2.776 → **not significant** at α=0.05)
- **Descriptive p (normal approx, df=4 underestimates):** 0.016 — directional but the small-sample t-test is the correct frame and it does not reach significance.

### 0d4ec3ad reproducibility

The 3oms +0.300 finding was driven by a single trial pair with baseline=0.500,
with-sg=0.800. At N=3:

- baseline mean ± std: 0.8000 ± 0.0000 (rewards: [0.8000, 0.8000, 0.8000])
- with-sg mean ± std: 0.8000 ± 0.0000 (rewards: [0.8000, 0.8000, 0.8000])
- delta: **0.000**

**Verdict: does not reproduce.** The 3oms 0.500 baseline score for 0d4ec3ad
appears to have been a single-trial regression that vanished when repeated.
With both configs now landing at the apparent task ceiling (0.800), there
is zero remaining headroom for Sourcegraph MCP to claim improvement.

## Determinism note

Reward variance across repeats is essentially zero for 4 of 5 tasks
(std=0.000); the only non-zero variance is on ba1f3675 with-sg
(std=0.0025, rewards alternate between 0.5388 and 0.5432). Cost and
wallclock vary substantially across repeats — runs ranged from ~430s
to ~2900s on identical inputs — but the resulting reward is reproducible.

This is consistent with the SDLC family being scored against a
deterministic test suite (`test.sh` based oracle): even when the agent
takes a different path or burns very different token budgets, the
end-state passes/fails the same set of tests, so the reward lands at
the same value. The N=3 repeats here therefore primarily measure
*scoring stability* rather than *agent stochasticity*. For these
particular tasks, the repeated trials are tight by construction, not
because the agent is deterministic.

## Cost summary

| config           | mean reward | tasks | total cost USD | $/trial |
|------------------|-------------|-------|----------------|---------|
| baseline         | 0.6831      | 15    | 69.11          | 4.61    |
| with-sourcegraph | 0.6866      | 15    | 85.17          | 5.68    |
| **total**        |             | 30    | **154.28**     |         |

The with-sg config overran the per-config $80 soft cap by ~$5.17 — the
cap is enforced post-hoc and the final trial in flight was allowed to
finish. Both numbers are well above the bead's nominal $90 estimate;
SDLC tasks are heavier than 3oms suggested.

## Acceptance

- A1 ✅ 30 trials produced (5 SDLC × 2 configs × N=3)
- A2 ✅ Per-task mean reward + std reported across 3 repeats (table above)
- A3 ✅ Per-family delta with 95% CI / paired-t test (CI: [−0.00053, +0.00743], t=2.41, df=4, n.s. at α=0.05)
- A4 ✅ 0d4ec3ad reproducibility check — does **not** reproduce (Δ=0)
- A5 ✅ This writeup

## Constraints honoured

- Private repo (gascity) only; no public push.
- Local commit on a feature branch off `main`.
- Run via standard codeprobe CLI; logs at `logs/run.{stdout,stderr}.log`.
- Soft cap enforced per-config; with-sg overran by ~$5, accepted per bead's
  "partial coverage if hit" allowance.

## Implications for the with-sourcegraph hypothesis

Combining 3oms (mixed-family N=1) with this rerun (SDLC N=3):

- 3oms claimed a SDLC family advantage of +0.054
- mcn7 tightens it to +0.0035, 95% CI just barely including zero
- The 0d4ec3ad outlier that drove most of the 3oms signal was noise
- No statistically significant family-level effect at α=0.05

**Recommendation:** Do not treat the 3oms SDLC delta as evidence that
Sourcegraph MCP helps on the SDLC family. The original +0.054 was
within single-trial noise on a 5-task panel. To test the MCP hypothesis
seriously, expand to either (a) more diverse families where MCP has a
plausible mechanism (e.g., cross-repo navigation tasks rather than
single-repo SDLC continuations), or (b) much higher N on a smaller set
of tasks where Sourcegraph's symbol search would matter.

## Files

- [`eval_writeup.md`](./eval_writeup.md) — this document.
- [`suite-sdlc-only.toml`](./suite-sdlc-only.toml) — task_ids filter for the 5 SDLC tasks.
- [`analyze.py`](./analyze.py) — analysis script (per-trial → per-task → per-family).
- [`per_trial.json`](./per_trial.json) — flat list of 30 trials with reward/cost/diagnostics.
- [`per_family_summary.json`](./per_family_summary.json) — per-task aggregates + family delta + 0d4ec3ad detail.
- [`logs/`](./logs) — successful attempt-3 stdout/stderr.
- [`logs.attempt-1/`](./logs.attempt-1) — partial run, mid-execution rate limit.
- [`logs.attempt-2/`](./logs.attempt-2) — full run, all trials hit pre-reset rate limit.
