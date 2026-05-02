# codeprobe-aupz — with-sg-fixed (combined evjr fixes) on SDLC + oracle_checks

**Status:** complete
**Branch:** `feature/codeprobe-x7p3-validate-unified-contract`
**Predecessor:** codeprobe-2txc, codeprobe-mcn7, codeprobe-ttwq, codeprobe-evjr.{1,2,3}, codeprobe-riad

## Purpose

codeprobe-evjr's cross-rig audit identified three structural causes for
codeprobe's MCP cost overhead (vs reference rigs CSB and EB). codeprobe-2txc
showed that the ovz2 preamble-tune alone did not fix `oc_004` (the failure
mode is a Sourcegraph false-negative cascade, not over-thoroughness).

This run measures whether the **combined** evjr fixes flatten the cost
pattern AND preserve / improve reward, against the same SDLC + oracle_checks
corpora used in mcn7 and ttwq.

## Configuration

`with-sg-fixed` includes ALL of:

- **evjr.1** — `--max-turns 50` cap on the claude adapter (matches EB rig)
- **evjr.2** — MCP-vs-local guardrail in the Sourcegraph preamble
  (`When To Use MCP vs Local Tools` table + "never use
  `mcp__sourcegraph__read_file` for files that exist locally" rule)
- **evjr.3 Part A** — `metadata.sg_repo` populated on SDLC tasks
  (manually backfilled to `github.com/gastownhall/gascity` on the 5 mcn7
  SDLC tasks; oc_001..oc_005 already had it)
- **evjr.3 Part B** — fail-loud guard in `task_preamble_context` (raises
  ValueError if the Sourcegraph preamble is requested with empty sg_repo)
- **ovz2** — `oracle_checks` and `sdlc` task_category branches in
  `task_preamble_context` (criterion-aware breadth on `oracle_checks`,
  navigation-narrow on `sdlc`)
- **riad** — verify-via-Grep before denying existence rule in
  `sg_negative_result_handling`

Cap value chosen: **`max_turns = 50`** (reference value EB uses; the
existing experiment.json had this set already after evjr.1 landed).

## Run setup

- **Target dir:** `/home/ds/test_repos/gascity/gascity-mcp-comparison/`
- **Tenant:** `codeprobe-aupz`
- **Model:** `claude-sonnet-4-6`
- **Concurrency:** `--parallel 2`
- **Repeats:** N=3 per task per config
- **Soft cap:** `--max-cost-usd 50` (SDLC), `--max-cost-usd 15` (OC)
- **Suites:**
  - `docs/investigations/codeprobe-aupz/suite-sdlc.toml` (5 SDLC tasks)
  - `docs/investigations/codeprobe-aupz/suite-oc.toml` (5 oc tasks)
- **Baselines reused** (per bead allowance — trial environment unchanged
  except for the codeprobe code-side fixes):
  - `mcn7` SDLC `baseline` + `with-sourcegraph` (15 + 15 trials)
  - `ttwq` oracle_checks `baseline` + `with-sourcegraph` (15 + 15 trials)

## Aggregate results

### Family-level rollup

| family | config | n | mean_reward | total_cost_usd | mean_time_s/trial | total_input_tok | total_output_tok | total_cache_read_tok |
|--------|--------|---|-------------|----------------|--------------------|-----------------|------------------|----------------------|
| sdlc | baseline (mcn7) | 15 | 0.6831 | $69.11 | 1118s | 1,247 | 600,745 | 152,102,759 |
| sdlc | with-sourcegraph (mcn7) | 15 | 0.6866 | $85.17 | 1889s | 45,047 | 1,739,453 | 148,388,201 |
| sdlc | **with-sg-fixed (this)** | 15 | **0.1548** | **$54.36** | **1363s** | 15,998 | 1,251,436 | 80,080,353 |
| oracle_checks | baseline (ttwq) | 15 | 1.000 | $4.63 | 70s | 111 | 27,262 | 2,951,942 |
| oracle_checks | with-sourcegraph (ttwq) | 15 | 0.914 | $5.23 | 97s | 634 | 76,725 | 5,047,122 |
| oracle_checks | **with-sg-fixed (this)** | 15 | **0.979** | **$3.32** | **72s** | 130 | 53,358 | 3,203,415 |

### Contrast 1 — SDLC: with-sg-fixed vs mcn7 with-sourcegraph

Paired delta on `mean_reward` (per-task, df=4):
**mean = -0.5317**, 95% CI [-0.917, -0.146], t = -3.83 → **statistically significant regression**

| task | metric | with-sg-fixed | with-sg(mcn7) | delta |
|------|--------|---------------|---------------|-------|
| ba1f3675 | reward (mean) | 0.0000 | 0.5403 | -0.5403 |
| ba1f3675 | wall-clock (mean/trial) | 1173s | 1444s | -271s |
| ba1f3675 | cost (total over 3) | $11.37 | $15.06 | -$3.69 |
| ba1f3675 | input tokens (total) | 4,788 | 17,045 | -12,257 |
| ba1f3675 | output tokens (total) | 204,463 | 264,598 | -60,135 |
| ba1f3675 | cache_read tokens (total) | 19,345,811 | 27,793,132 | -8,447,321 |
| d906ac3d | reward (mean) | 0.5643 | 0.6079 | -0.0436 |
| d906ac3d | wall-clock (mean/trial) | 968s | 1102s | -134s |
| d906ac3d | cost (total over 3) | $7.53 | $6.86 | +$0.67 |
| d906ac3d | output tokens (total) | 181,671 | 205,618 | -23,947 |
| d906ac3d | cache_read tokens (total) | 10,130,273 | 7,130,015 | +3,000,258 |
| 0d4ec3ad | reward (mean) | 0.0000 | 0.8000 | -0.8000 |
| 0d4ec3ad | wall-clock (mean/trial) | 855s | 2311s | -1,456s |
| 0d4ec3ad | cost (total over 3) | $8.88 | $19.84 | -$10.96 |
| 0d4ec3ad | output tokens (total) | 136,958 | 396,237 | -259,279 |
| 0d4ec3ad | cache_read tokens (total) | 15,856,438 | 34,906,384 | -19,049,946 |
| 45b581b5 | reward (mean) | 0.0000 | 0.8000 | -0.8000 |
| 45b581b5 | wall-clock (mean/trial) | 1778s | 2334s | -556s |
| 45b581b5 | cost (total over 3) | $13.52 | $15.14 | -$1.61 |
| 45b581b5 | output tokens (total) | 328,091 | 406,510 | -78,419 |
| 45b581b5 | cache_read tokens (total) | 19,840,774 | 19,923,784 | -83,010 |
| fde8e6e0 | reward (mean) | 0.2099 | 0.6846 | -0.4747 |
| fde8e6e0 | wall-clock (mean/trial) | 2042s | 2257s | -215s |
| fde8e6e0 | cost (total over 3) | $13.06 | $28.28 | -$15.22 |
| fde8e6e0 | output tokens (total) | 400,253 | 466,490 | -66,237 |
| fde8e6e0 | cache_read tokens (total) | 14,907,057 | 58,634,886 | -43,727,829 |

### Contrast 2 — SDLC: with-sg-fixed vs mcn7 baseline

Paired delta on `mean_reward` (per-task, df=4):
**mean = -0.5283**, 95% CI [-0.916, -0.140], t = -3.78 → **statistically significant regression**

| task | metric | with-sg-fixed | baseline(mcn7) | delta |
|------|--------|---------------|----------------|-------|
| ba1f3675 | reward (mean) | 0.0000 | 0.5340 | -0.5340 |
| ba1f3675 | wall-clock (mean/trial) | 1173s | 1285s | -112s |
| ba1f3675 | cost (total over 3) | $11.37 | $18.48 | -$7.12 |
| d906ac3d | reward (mean) | 0.5643 | 0.6031 | -0.0388 |
| d906ac3d | wall-clock (mean/trial) | 968s | 626s | +342s |
| d906ac3d | cost (total over 3) | $7.53 | $4.57 | +$2.96 |
| 0d4ec3ad | reward (mean) | 0.0000 | 0.8000 | -0.8000 |
| 0d4ec3ad | wall-clock (mean/trial) | 855s | 1842s | -987s |
| 0d4ec3ad | cost (total over 3) | $8.88 | $26.17 | -$17.29 |
| 45b581b5 | reward (mean) | 0.0000 | 0.8000 | -0.8000 |
| 45b581b5 | wall-clock (mean/trial) | 1778s | 444s | +1,334s |
| 45b581b5 | cost (total over 3) | $13.52 | $4.52 | +$9.00 |
| fde8e6e0 | reward (mean) | 0.2099 | 0.6784 | -0.4685 |
| fde8e6e0 | wall-clock (mean/trial) | 2042s | 1393s | +648s |
| fde8e6e0 | cost (total over 3) | $13.06 | $15.37 | -$2.31 |

### Contrast 3 — oracle_checks: with-sg-fixed vs ttwq with-sourcegraph

Paired delta on `mean_reward` (per-task, df=4):
**mean = +0.0643**, 95% CI [-0.140, +0.268], t = +0.87 → null effect overall, **but oc_004 individually +0.357**

| task | metric | with-sg-fixed | with-sg(ttwq) | delta |
|------|--------|---------------|---------------|-------|
| oc_001 | reward (mean) | 1.0000 | 1.0000 | 0.0000 |
| oc_001 | wall-clock (mean/trial) | 172s | 139s | +33s |
| oc_001 | cost (total over 3) | $1.50 | $1.39 | +$0.11 |
| oc_002 | reward (mean) | 1.0000 | 1.0000 | 0.0000 |
| oc_002 | wall-clock (mean/trial) | 28s | 22s | +5s |
| oc_002 | cost (total over 3) | $0.23 | $0.30 | -$0.07 |
| oc_003 | reward (mean) | 0.9643 | 1.0000 | -0.0357 |
| oc_003 | wall-clock (mean/trial) | 60s | 214s | -154s |
| oc_003 | cost (total over 3) | $0.55 | $1.87 | -$1.32 |
| oc_003 | output tokens (total) | 7,490 | 33,244 | -25,754 |
| **oc_004** | **reward (mean)** | **0.9286** | **0.5714** | **+0.3571** |
| oc_004 | wall-clock (mean/trial) | 61s | 76s | -15s |
| oc_004 | cost (total over 3) | $0.57 | $1.16 | -$0.59 |
| oc_004 | output tokens (total) | 8,981 | 10,301 | -1,320 |
| oc_005 | reward (mean) | 1.0000 | 1.0000 | 0.0000 |
| oc_005 | wall-clock (mean/trial) | 41s | 36s | +5s |
| oc_005 | cost (total over 3) | $0.46 | $0.51 | -$0.05 |

### Contrast 4 — oracle_checks: with-sg-fixed vs ttwq baseline

Paired delta on `mean_reward` (per-task, df=4):
**mean = -0.0214**, 95% CI [-0.061, +0.018], t = -1.50 → null effect (within noise)

| task | metric | with-sg-fixed | baseline(ttwq) | delta |
|------|--------|---------------|----------------|-------|
| oc_001 | reward (mean) | 1.0000 | 1.0000 | 0.0000 |
| oc_001 | cost (total over 3) | $1.50 | $1.43 | +$0.07 |
| oc_002 | reward (mean) | 1.0000 | 1.0000 | 0.0000 |
| oc_002 | cost (total over 3) | $0.23 | $0.52 | -$0.30 |
| oc_003 | reward (mean) | 0.9643 | 1.0000 | -0.0357 |
| oc_003 | cost (total over 3) | $0.55 | $0.89 | -$0.34 |
| oc_004 | reward (mean) | 0.9286 | 1.0000 | -0.0714 |
| oc_004 | cost (total over 3) | $0.57 | $0.95 | -$0.38 |
| oc_005 | reward (mean) | 1.0000 | 1.0000 | 0.0000 |
| oc_005 | cost (total over 3) | $0.46 | $0.83 | -$0.37 |

## A3 specific checks

- **SDLC mean wall-clock <1300s/trial** (mcn7 saw 1890s under raw with-sg):
  ❌ **MISSED narrowly** — 1363s. Better than mcn7's 1889s but still
  above the target. Confounded by 13/15 trials hitting the turn cap mid-edit
  (so wall-clock is "agent burned all 50 turns" rather than "agent finished
  efficiently").
- **SDLC output tokens <800k total/15 trials** (mcn7 saw 1.74M):
  ❌ **MISSED** — 1.25M. Better than mcn7's 1.74M (-28%) but still well above
  baseline's 600k. Same confound: failing trials still emitted ~100k+ output
  tokens per session before being cut off.
- **SDLC cost <$75** (mcn7 saw $85): ✓ **MET** — $54.36, well under target,
  even below mcn7's baseline ($69.11).
- **oracle_checks oc_004 `names_toml_tag` criterion 3/3** (vs 0/3 under
  raw with-sg AND ovz2-tuned): ✓ **MET** — `names_toml_tag` = 1.0 across
  all 3 oc_004 trials. The Sourcegraph false-negative cascade that previously
  caused the agent to incorrectly deny the `toml:` tag's existence is gone.
  oc_004 now passes 2/3 trials at full reward; the one partial (0.7857)
  missed `names_resolve_path` only.

## A4 — max-turns hit rate

**Cap value: 50.**

| family | trials hitting cap | total | rate |
|--------|--------------------|-------|------|
| sdlc | 13 | 15 | **86.7 %** |
| oracle_checks | 0 | 15 | 0.0 % |

Per-task SDLC breakdown:

| task | repeat 0 | repeat 1 | repeat 2 |
|------|----------|----------|----------|
| 0d4ec3ad | hit (r=0.0) | hit (r=0.0) | hit (r=0.0) |
| 45b581b5 | hit (r=0.0) | hit (r=0.0) | hit (r=0.0) |
| ba1f3675 | hit (r=0.0) | hit (r=0.0) | hit (r=0.0) |
| d906ac3d | finished (r=0.564) | finished (r=0.564) | finished (r=0.564) |
| fde8e6e0 | hit (r=0.0) | hit (r=0.0) | finished (r=0.630) |

**Every trial that hit the cap landed at `reward=0.0`** — the agent was cut
off mid-edit and never produced a complete change set the verifier could
score. Trials that finished within 50 turns scored within mcn7-baseline-and-
with-sourcegraph noise (d906ac3d 0.564 vs 0.603 / 0.608; fde8e6e0 0.630 vs
0.678 / 0.685).

## Verdict (A6)

The combined fixes split sharply by family:

### oracle_checks — ROLL AS DEFAULTS

- Reward back to baseline-equivalent (0.979 vs baseline 1.000, vs raw with-sg
  0.914) — paired CI vs raw with-sg [-0.140, +0.268] is null, but oc_004
  individually moves +0.357 (the failure mode the bead was chasing).
- Cost down to **$3.32** vs raw with-sg's $5.23 (-37%) and even below baseline's
  $4.63 (-28%) — the MCP-vs-local guardrail (evjr.2) cut wasted Sourcegraph
  reads on locally-available files; the riad verify-via-Grep rule rescued
  oc_004's denial cascade; max-turns=50 was never approached because oc tasks
  are short (mean 72s).
- Wall-clock back to **72s/trial** vs raw with-sg's 97s and matching baseline's
  70s.
- **No follow-up needed** — recommend rolling these defaults out for the
  oracle_checks family.

### sdlc — DO NOT ROLL UNTIL `max-turns` IS RAISED OR REMOVED FOR SDLC

- Reward collapsed from 0.683 (baseline) / 0.687 (raw with-sg) to **0.155**
  with-sg-fixed — paired CI vs both [-0.917, -0.146] excludes 0; this is a
  4-of-5-task wipeout, not noise.
- The cause is unambiguous: **13/15 trials (87%) hit `error_max_turns` at
  num_turns=51** and were terminated before producing a complete edit. mcn7's
  uncapped with-sourcegraph runs averaged ~60-90 turns on the same SDLC
  tasks; 50 is too tight for this family.
- Cost ($54) and wall-clock (1363s) "improved" only because the cap kills
  trials early — this is not a real efficiency win; the agent was making
  forward progress when terminated.

### Follow-up beads to file

1. **codeprobe-aupz.1 — SDLC max-turns retune.** Rerun 5 SDLC × N=3 with
   `max_turns ∈ {75, 90, none}` to find the smallest cap that does not
   collapse reward. EB rig uses 50 because EB tasks are scoped narrowly;
   gascity SDLC tasks edit ~3-5 files in interrelated services and need
   more turns. Likely answer: cap should be task-category-aware
   (oracle_checks: 50; sdlc: 90+ or unbounded).
2. **codeprobe-aupz.2 — make `--max-turns` task-category-aware.** Surface
   it as `task.toml` metadata (`max_turns_override = N`) so the adapter
   reads from the task rather than the global flag, with a per-family
   default.
3. **(stretch)** codeprobe-aupz.3 — investigate whether the agent could
   emit a "save partial progress" checkpoint when the turn budget shows
   `<= 5 remaining` so cap-cutoff trials at least produce verifier-scorable
   edits rather than 0.0.

## Files

- [`suite-sdlc.toml`](./suite-sdlc.toml) — 5 SDLC task IDs
- [`suite-oc.toml`](./suite-oc.toml) — 5 oc task IDs
- [`analyze.py`](./analyze.py) — joins aupz scoring.json with mcn7+ttwq
  per_trial.json and emits the four contrasts
- [`per_trial.json`](./per_trial.json) — flat with-sg-fixed trial table
  (5 + 5 = 30 trials)
- [`per_family_summary.json`](./per_family_summary.json) — per-task,
  family, contrast aggregates with 95% CIs
- [`analyze.out`](./analyze.out) — full stdout from `python analyze.py`
- [`logs-sdlc/`](./logs-sdlc) — SDLC run stdout/stderr
- [`logs-oc/`](./logs-oc) — oc run stdout/stderr
