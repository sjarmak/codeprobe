# codeprobe-4cl6.1 — with-sg-cap75: SDLC max-turns retune, cap=75 sweep point

**Status:** complete
**Bead:** `codeprobe-4cl6.1` (child of `codeprobe-4cl6`)
**Predecessor:** `codeprobe-aupz` (`with-sg-fixed`, cap=50 — writeup at
`docs/investigations/codeprobe-aupz/eval_writeup.md`)

## Purpose

aupz showed `--max-turns=50` collapses SDLC reward (0.155 vs mcn7 baseline
0.683): 13/15 trials hit `error_max_turns` and scored 0.0. This sweep point
measures whether 75 turns is enough headroom, on the same 5 SDLC tasks ×
N=3 trials.

## Configuration

`with-sg-cap75` = aupz `with-sg-fixed` (sourcegraph MCP + preamble v2 +
MCP-vs-local guardrail + populated sg_repo + ovz2 oracle_checks/sdlc
branches + riad verify-via-local-Grep — all on main) with two deltas:

- `max_turns = 75` (was 50)
- `extra.timeout_seconds = 5400` (was default 3600) — so the per-task
  timeout cannot confound the cap measurement. Justified post-hoc: three
  trials ran 2,900–3,100s; none hit the timeout.

Run setup otherwise identical to aupz: target
`/home/ds/test_repos/gascity/gascity-mcp-comparison/`, tenant
`codeprobe-4cl6`, model `claude-sonnet-4-6`, `--parallel 2`,
`--max-cost-usd 50` per invocation,
suite `docs/investigations/codeprobe-aupz/suite-sdlc.toml`.

## Run forensics (three invocations, one logical run)

1. **run1** (logs `logs-sdlc/run.*.log`) — poisoned by OAuth session-limit
   exhaustion at ~12:16 ET. The 2026-06 wording ("You've hit your session
   limit") did not match `_QUOTA_PATTERN`, so the executor kept dispatching:
   12 trials became 1–2s stubs scored 0.0 under an `ok:true` envelope, and
   2 real trials were truncated mid-flight without a result record. Only one
   trial survived as a valid sample (0d4ec3ad r1, a genuine 76-turn cap-hit).
   Fixed in commit `cc2cd3a` (quota regex), active for runs 2–3.
   Spend $16.53, of which $6.38 was the kept trial.
2. **run2** (`run2.*.log`) — checkpoint resume of the 14 lost trials; halted
   on the $50 budget after 10 new trials. The 10 new trials sum to $59.86
   (the runner's halt message; overshoot from in-flight parallelism — same
   behavior as aupz). The run2 envelope reports $66.25/11 tasks because it
   also counts the checkpoint-merged run1 trial ($6.38).
3. **run3** (`run3.*.log`) — checkpoint resume of the last 4 trials under
   Stephanie's top-up authorization ($18.12). Final envelope: 15/15 trials,
   mean_score 0.5071, config cost $84.37.

**Checkpoint caveat (manual intervention):** `CheckpointStore.load_ids`
excludes `status='error'` rows so errored trials get retried — but genuine
`error_max_turns` trials (the very thing this sweep measures) are also
stored as `status='error'`. Naive resume would have discarded and re-run 4
valid cap-hit samples (~$24). I distinguished them by auditing each error
trial's `agent_output.txt`: a terminal `type:result` record with
`subtype=error_max_turns` ⇒ genuine sample, flipped to `'completed'` in
checkpoint.db before resuming; no result record ⇒ infra casualty, left for
retry. Follow-up bead filed: `codeprobe-8up` ([4cl6.f1]) (see below).

**Cost accounting:** kept 15 trials = $84.37; total real spend = $94.51
($16.53 + $59.86 + $18.12); quota/truncation waste = $10.14.

## Aggregate results

| config | n | mean_reward | cap-hit rate | total_cost | mean_time/trial | total output tok |
|--------|---|-------------|--------------|------------|------------------|------------------|
| mcn7 baseline (no MCP, uncapped) | 15 | 0.6831 | 0% | $69.11 | 1118s | 600,745 |
| mcn7 with-sourcegraph (uncapped) | 15 | 0.6866 | 0% | $85.17 | 1889s | 1,739,453 |
| aupz with-sg-fixed (cap=50) | 15 | 0.1548 | **86.7%** | $54.36 | 1363s | 1,251,436 |
| **with-sg-cap75 (this)** | 15 | **0.5071** | **26.7%** | $84.37 | 2036s | 1,662,179 |

## Per-trial detail

| task | repeat | reward | result | num_turns | tool_calls | wall-clock | cost | output tok |
|------|--------|--------|--------|-----------|------------|------------|------|------------|
| ba1f3675 | 0 | 0.7878 | success | — | 74 | 1752s | $5.48 | 105,312 |
| ba1f3675 | 1 | 0.7875 | success | — | 109 | 1954s | $6.94 | 109,968 |
| ba1f3675 | 2 | 0.7866 | success | — | 72 | 1276s | $4.48 | 72,038 |
| d906ac3d | 0 | 0.5661 | success | — | 80 | 3083s | $7.38 | 169,595 |
| d906ac3d | 1 | 0.5640 | success | — | 50 | 2072s | $5.17 | 110,777 |
| d906ac3d | 2 | 0.5645 | success | — | 18 | 1106s | $2.53 | 59,393 |
| 0d4ec3ad | 0 | 0.0000 | error_max_turns | 76 | 120 | 1356s | $5.91 | 57,685 |
| 0d4ec3ad | 1 | 0.0000 | error_max_turns | 76 | 103 | 2078s | $6.38 | 97,971 |
| 0d4ec3ad | 2 | 0.0000 | error_max_turns | 76 | 102 | 1862s | $6.08 | 87,182 |
| 45b581b5 | 0 | 0.8296 | success | — | 103 | 2613s | $6.55 | 128,216 |
| 45b581b5 | 1 | 0.6305 | success | — | 59 | 2671s | $5.77 | 152,325 |
| 45b581b5 | 2 | 0.0000 | error_max_turns | 76 | 83 | 2327s | $6.11 | 124,185 |
| fde8e6e0 | 0 | 0.6301 | success | — | 63 | 2960s | $6.61 | 177,076 |
| fde8e6e0 | 1 | 0.6297 | success | — | 62 | 1735s | $4.70 | 106,762 |
| fde8e6e0 | 2 | 0.8297 | success | — | 66 | 1692s | $4.28 | 103,694 |

`num_turns` is only persisted for error-terminated trials (the adapter
stores the raw CLI result record only on error; for successes
`agent_output.txt` holds the final answer text). `tool_calls` from
results.json is the activity proxy for finished trials. Tracked in
follow-up `codeprobe-8up` ([4cl6.f1]).

## Max-turns hit rate (the bead's A4-equivalent)

**4/15 = 26.7%** (was 13/15 = 86.7% at cap=50). Per-task map:

| task | repeat 0 | repeat 1 | repeat 2 |
|------|----------|----------|----------|
| ba1f3675 | finished (r=0.788) | finished (r=0.787) | finished (r=0.787) |
| d906ac3d | finished (r=0.566) | finished (r=0.564) | finished (r=0.565) |
| 0d4ec3ad | hit @76 (r=0.0) | hit @76 (r=0.0) | hit @76 (r=0.0) |
| 45b581b5 | finished (r=0.830) | finished (r=0.630) | hit @76 (r=0.0) |
| fde8e6e0 | finished (r=0.630) | finished (r=0.630) | finished (r=0.830) |

As in aupz, **every cap-hit trial scored 0.0** — termination mid-edit never
produces a scoreable change set. Reward per task is bimodal in the cap:
either the task fits in the budget and scores at its natural level, or it
doesn't and scores 0.

## Paired contrasts (per-task means, df=4, t_crit=2.776)

### cap75 vs aupz cap50 (primary)

**mean Δ = +0.3522**, 95% CI **[−0.075, +0.779]**, t = 2.29 → large positive
but not significant at 95%. The delta distribution is bimodal: three tasks
recovered massively (ba1f3675 +0.787, 45b581b5 +0.487, fde8e6e0 +0.487),
two were structurally unchanged (d906ac3d +0.0005 — it never hit either
cap; 0d4ec3ad 0.0 — it hits both caps 3/3). With n=5 and that variance
shape, the paired t is underpowered; the per-task picture is unambiguous.

### cap75 vs mcn7 baseline (parent A3 reference)

**mean Δ = −0.1760**, 95% CI **[−0.676, +0.324]**, t = −0.98 → CI contains
0. Formally, cap=75 already meets the parent's A3 criterion ("paired CI for
reward vs mcn7 baseline contains 0"). **Caveat:** this is partly low power,
not full recovery — 0d4ec3ad is still a 3/3 cap-hit losing 0.80 reward vs
baseline. ba1f3675 (+0.25) partially offsets it in the mean.

### cap75 vs mcn7 with-sourcegraph (uncapped)

**mean Δ = −0.1795**, 95% CI [−0.676, +0.317], t = −1.00 → same shape as
the baseline contrast (mcn7's two configs scored nearly identically).

## Observations

1. **Tasks finishing under cap=75 score at or above their uncapped levels.**
   ba1f3675 at 0.787±0.001 beats both mcn7 arms (~0.54); fde8e6e0 0.696 vs
   0.685/0.678; d906ac3d unchanged at 0.564–0.566. The evjr/ovz2/riad fixes
   are not the constraint — the turn budget is.
2. **0d4ec3ad needs more than 75 turns under the with-sg config.** All three
   trials burned the full budget (76 recorded turns, 102–120 tool calls).
   mcn7's uncapped with-sg run scored 0.80 on it at ~2311s/trial, so it is
   solvable — the cap90/uncapped arms will show where it clears.
3. **45b581b5 sits at the boundary** (2/3 finished at 0.83/0.63; the third
   hit the cap) — at cap=50 it was 0/3. Expect 3/3 completion at cap=90.
4. **Cost scales with the cap for cap-hitting tasks**: $84.37 vs aupz's
   $54.36 (+55%), with mean wall-clock 2036s vs 1363s. Capped failures are
   expensive failures: the 4 cap-hits cost $24.48 for 0 reward.

## Verdict

cap=75 recovers the with-sg SDLC family from 0.155 to 0.507 (cap-hit rate
86.7% → 26.7%) and formally satisfies the parent's A3 CI-overlap criterion
against mcn7 baseline, but one of five tasks (0d4ec3ad) still deterministically
exhausts the budget and scores 0. **75 is not the recommended default; it is
a lower bound.** The cap90 (`codeprobe-4cl6.2`) and uncapped
(`codeprobe-4cl6.3`) arms determine whether 90 clears 0d4ec3ad or whether
the SDLC default should be unbounded; the parent's A4 recommendation is
deferred to the cross-arm analysis after all three children land.

## Acceptance check (bead `codeprobe-4cl6.1`)

- [x] 15 trials complete; envelopes archived under `runs/codeprobe-4cl6/`
      (project-side archive) and live under
      `gascity-mcp-comparison/.codeprobe/runs/with-sg-cap75/`
- [x] Per-trial `error_max_turns` rate computed (26.7%); pair-test vs aupz
      `with-sg-fixed` cap=50 (+0.352, CI [−0.075, +0.779])
- [x] Writeup at `docs/investigations/codeprobe-4cl6/cap75.md`
- [x] Commit on main; bead close metadata set

## Artifacts

- `docs/investigations/codeprobe-4cl6/per_trial.json` — 15 normalized trials
- `docs/investigations/codeprobe-4cl6/per_family_summary.json` — rollup + contrasts
- `docs/investigations/codeprobe-4cl6/analyze.py` / `analyze.out` — deterministic analyzer (ZFC-compliant)
- `docs/investigations/codeprobe-4cl6/logs-sdlc/run{,2,3}.{stdout,stderr}.log` — all three invocations
- `runs/codeprobe-4cl6/` — archived envelopes (run-id-level copy)
- Raw run dirs: `/home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe/runs/with-sg-cap75/`
