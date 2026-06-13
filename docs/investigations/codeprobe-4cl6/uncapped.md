# codeprobe-4cl6.3 — with-sg-uncapped: SDLC max-turns retune, uncapped control

**Status:** complete
**Bead:** `codeprobe-4cl6.3` (child of `codeprobe-4cl6`)
**Siblings:** `codeprobe-4cl6.1` (`with-sg-cap75` — `cap75.md`),
`codeprobe-4cl6.2` (`with-sg-cap90` — `cap90.md`)
**Predecessor:** `codeprobe-aupz` (`with-sg-fixed`, cap=50)

## Purpose

This is the **ceiling** arm of the max-turns sweep. cap75 (0.507) and cap90
(0.502) both recovered the with-sg SDLC family from the cap=50 collapse
(0.155) but left `0d4ec3ad` failing 2–3/3 by exhausting the turn budget, and
neither cap improved on the other. The open question both capped writeups
deferred: **does the family need an *unbounded* turn budget to recover
`0d4ec3ad`, or is that task intractable for this with-sg config regardless of
cap?** This arm removes `--max-turns` entirely and measures whether reward
returns to mcn7's uncapped with-sourcegraph level (~0.687). Same 5 SDLC tasks
× N=3.

## Configuration

`with-sg-uncapped` = aupz `with-sg-fixed` (sourcegraph MCP + preamble v2 +
MCP-vs-local guardrail + populated sg_repo + ovz2 oracle_checks/sdlc branches
+ riad verify-via-local-Grep) with:

- `max_turns = null` — **no turn cap** (cap75 used 75; cap90 used 90; aupz 50).
  Confirmed loader-equivalent to omitting the flag: `adapters/claude.py`
  skips `--max-turns` when the value is `None`.
- `extra.timeout_seconds = 5400` — same 90-minute wall-clock guard as
  cap75/cap90, retained so the turn variable is the only difference across the
  sweep. **This guard was assumed non-binding** (the config note cites mcn7
  with-sg's longest trial at 2913s) — see the forensics caveat below, where it
  did in fact clip one trial.

Run setup otherwise identical to the capped sweeps: target
`/home/ds/test_repos/gascity/gascity-mcp-comparison/`, tenant
`codeprobe-4cl6`, model `claude-sonnet-4-6`, `--parallel 2`, suite
`docs/investigations/codeprobe-aupz/suite-sdlc.toml`.

## Run forensics (single clean invocation)

One launch, no resume cycles, soft cap never fired. pid 3806970 launched
2026-06-13 00:37 ET under `setsid nohup` (`launch-uncapped.sh`), all 15 work
items dispatched fresh (no checkpoint reuse — verified: zero "Skipping" /
"resume" / "budget exceeded" markers in the logs). `--max-cost-usd 90`; the
run finished at $88.70 total, **below** the soft cap, so unlike cap90 there
was no halt-and-resume tail. Terminal envelope `exit_code 0, ok true`,
15/15 `task_done` events banked. Wall-clock ~5h.

**Cost accounting:** $88.70 over the 14 cost-bearing trials. The 15th
(`0d4ec3ad` r2) recorded no cost/token telemetry because it terminated on the
wall-clock timeout before emitting a CLI result record (see below). Comparable
to mcn7 with-sg's $85.17.

**Telemetry provenance:** `num_turns` and per-trial cost/tokens are read from
each trial's `scoring.json` `diagnostics` block (the adapter's banked
stream-json telemetry), not from `agent_output.txt` — which in this run holds
the agent's plain-text final answer, not a stream-json result line. The
analyzer was corrected to read the authoritative diagnostics source; the
earlier agent_output parse path is retained only as a legacy fallback.

## Aggregate results

| config | n | mean_reward | cap-hit rate | total_cost | mean_time/trial | total output tok |
|--------|---|-------------|--------------|------------|------------------|------------------|
| mcn7 baseline (no MCP, uncapped) | 15 | 0.6831 | 0% | $69.11 | 1118s | 600,745 |
| mcn7 with-sourcegraph (uncapped) | 15 | 0.6866 | 0% | $85.17 | 1889s | 1,739,453 |
| aupz with-sg-fixed (cap=50) | 15 | 0.1548 | 86.7% | $54.36 | 1363s | 1,251,436 |
| with-sg-cap75 (`4cl6.1`) | 15 | 0.5071 | 26.7% | $84.37 | 2036s | 1,662,179 |
| with-sg-cap90 (`4cl6.2`) | 15 | 0.5015 | 33.3% | $99.64 | 2382s | 1,894,350 |
| **with-sg-uncapped (this)** | 15 | **0.6592** | **0%** (n/a) | $88.70 | 2355s | 1,601,617 |

`mean_reward` is the mean of per-task means (each task weighted equally). The
"cap-hit rate" column is not applicable to the uncapped arm — there is no turn
cap to hit — but **one trial still scored 0.0**, via wall-clock timeout, not
turn exhaustion (see below).

## Per-trial detail

| task | repeat | reward | result | num_turns | wall-clock | cost | output tok |
|------|--------|--------|--------|-----------|------------|------|------------|
| ba1f3675 | 0 | 0.5881 | success | 153 | 4332s | $11.57 | 226,406 |
| ba1f3675 | 1 | 0.5881 | success | 62 | 1584s | $4.57 | 84,265 |
| ba1f3675 | 2 | 0.7866 | success | 72 | 894s | $3.55 | 43,377 |
| d906ac3d | 0 | 0.5640 | success | 36 | 1666s | $3.85 | 92,626 |
| d906ac3d | 1 | 0.5640 | success | 30 | 1341s | $3.20 | 73,523 |
| d906ac3d | 2 | 0.7634 | success | 64 | 1753s | $5.59 | 85,354 |
| 0d4ec3ad | 0 | 0.8267 | success | 171 | 2800s | $10.27 | 125,479 |
| 0d4ec3ad | 1 | 0.8276 | success | 180 | 2592s | $10.01 | 124,645 |
| 0d4ec3ad | 2 | 0.0000 | timeout (wall-clock) | — | 5400s | — | — |
| 45b581b5 | 0 | 0.8296 | success | 123 | 1617s | $6.43 | 85,272 |
| 45b581b5 | 1 | 0.6305 | success | 67 | 2179s | $5.12 | 119,513 |
| 45b581b5 | 2 | 0.8296 | success | 69 | 1276s | $3.85 | 67,993 |
| fde8e6e0 | 0 | 0.6297 | success | 88 | 2200s | $6.43 | 140,405 |
| fde8e6e0 | 1 | 0.6309 | success | 99 | 3774s | $8.84 | 210,783 |
| fde8e6e0 | 2 | 0.8297 | success | 73 | 1920s | $5.42 | 121,976 |

`0d4ec3ad` r2 stored `status='error'` with only `task_time_seconds=5400.12`
(exactly the 90-minute timeout) and no cost/token/turn telemetry — the
signature of a wall-clock kill, not a model-emitted error. It is the lone 0.0
in the arm.

## Turn distribution (with-sg-uncapped)

n=14 (the timeout trial has no recorded turn count):

| stat | value |
|------|-------|
| min | 30 |
| Q1 | 63.5 |
| median | **72.5** |
| Q3 | 130.5 |
| max | 180 |
| mean | 91.9 |
| in 60–90 band | 7/14 |
| over 75 | 6/14 |
| over 90 | 5/14 |

Sorted: `30, 36, 62, 64, 67, 69, 72, 73, 88, 99, 123, 153, 171, 180`.

The **median (72.5) sits squarely inside the bead's expected ~60–90-turn
band**, confirming aupz's reading of mcn7 against direct measurement. But the
distribution has a **fat right tail**: 5/14 trials exceeded 90 turns and 4
exceeded 120 (123, 153, 171, 180). Those tail trials are exactly the ones the
caps were truncating — which is the whole story of this sweep (next section).

## The `0d4ec3ad` finding (the question the capped arms deferred)

This is the load-bearing result. cap75 hit the turn cap on `0d4ec3ad` 3/3
(all 0.0); cap90 hit it 2/3 (all 0.0). Uncapped:

| arm | 0d4ec3ad outcome | turns | mean reward |
|-----|------------------|-------|-------------|
| cap=50 | 3/3 cap-hit | 51 | 0.000 |
| cap=75 | 3/3 cap-hit | 76 | 0.000 |
| cap=90 | 2/3 cap-hit, 1 success | 91 / 99 | 0.275 |
| **uncapped** | **2/3 success, 1 wall-clock timeout** | **171, 180** | **0.551** |

`0d4ec3ad` is **solvable but turn-expensive**: it succeeds at 0.827/0.828 —
but only when allowed **171 and 180 turns**, roughly **2× the cap90 limit**.
That is the direct mechanistic explanation for why every capped sweep
truncated it at 0.0: the cap, not the task, was the binding constraint. The
third repeat still failed (0.0), but it ran out of *wall-clock* (5400s), not
turns — so even with turns unbounded, `0d4ec3ad` does not reach 3/3, and its
uncapped per-task mean (0.551) sits ~0.25 below mcn7 with-sg's 0.80 on this
task. mcn7's uncapped 0.80 came from runs without the 90-minute wall-clock
guard this sweep imposed (see caveat).

## Paired contrasts (per-task means, df=4, t_crit=2.776)

### uncapped vs mcn7 with-sourcegraph (the bead's primary criterion)

**mean Δ = −0.0273**, 95% CI **[−0.195, +0.140]**, t = −0.45 →
**statistically indistinguishable**. The acceptance criterion ("reward
statistically indistinguishable from mcn7's with-sg baseline, paired-t check")
is **met** — the CI straddles 0 comfortably. Per-task deltas: `ba1f3675`
+0.114, `d906ac3d` +0.023, `0d4ec3ad` −0.249, `45b581b5` −0.037, `fde8e6e0`
+0.012. The single large negative (`0d4ec3ad`, from its 1/3 wall-clock
timeout) is offset by `ba1f3675` running slightly hotter than mcn7's sample;
the rest are within noise.

### uncapped vs mcn7 baseline (no MCP)

**mean Δ = −0.0239**, 95% CI **[−0.195, +0.147]**, t = −0.39 → indistinguishable.
Removing the cap returns the with-sg family to mcn7's no-MCP baseline level as
well — the sourcegraph machinery neither helps nor hurts net reward at this
sample size once the cap is gone.

### uncapped vs aupz cap50

**mean Δ = +0.5044**, 95% CI **[+0.174, +0.835]**, t = 4.23 → **strong,
significant recovery** (CI excludes 0, unlike cap75/cap90 whose vs-cap50 CIs
just grazed 0). Uncapping lifts the family 0.50 reward over the cap=50
collapse — the largest and only statistically clean recovery in the sweep.

## Cross-arm summary (the sweep curve)

| cap | mean_reward | vs uncapped Δ | cap-hits | cost | verdict |
|-----|-------------|---------------|----------|------|---------|
| 50 | 0.155 | −0.504 | 86.7% | $54 | collapsed |
| 75 | 0.507 | −0.152 | 26.7% | $84 | recovered, plateau |
| 90 | 0.502 | −0.157 | 33.3% | $100 | no gain over 75 |
| ∞ | **0.659** | — | 0% | $89 | ceiling |

The curve rises steeply from 50→75, is **flat 75→90**, then rises again
75/90→∞. The 75→∞ gap (+0.15) is **not** statistically separable at N=3 (the
cap90-vs-uncapped and cap75-vs-uncapped per-task CIs both include 0), but it
is consistent in sign and concentrated almost entirely on `0d4ec3ad` — the one
task whose successful trajectories need 170–180 turns. For the other four
tasks, 75 turns already captures essentially all the available reward.

## Observations

1. **The cap *was* the binding constraint on `0d4ec3ad`, not task
   intractability.** It succeeds (0.827) given 171–180 turns — ~2× cap90.
   This answers the question `cap90.md` left open: the family does need a
   budget well above 90 turns to recover that task, but recovery is partial
   (2/3, with the third trial lost to the wall-clock guard).
2. **Uncapped reward matches the mcn7 ceiling** (Δ = −0.027 vs with-sg, −0.024
   vs baseline; both CIs straddle 0). The retune's premise — that the cap, not
   the with-sg config, was suppressing reward — holds.
3. **Cost is not the reason to prefer a cap.** Uncapped ran *cheaper* than
   cap90 ($88.70 vs $99.64) because it had no expensive cap-hit failures
   (cap90 spent $40 on 5 zero-reward cap-hits) and no resume overhead. The
   trade is latency/variance, not dollars: the uncapped tail includes a 4332s
   success and a 5400s timeout.
4. **Most tasks don't use the headroom.** 9/14 trials finished in ≤90 turns;
   the median is 72.5. The uncapped budget matters for exactly one task
   (`0d4ec3ad`) and partially for `ba1f3675`/`45b581b5` (one tail trial each at
   123/153 turns).

## Forensics caveat — the wall-clock guard was not fully non-binding

The config assumed `timeout_seconds=5400` would not clip the uncapped
distribution, citing mcn7 with-sg's 2913s maximum. That assumption was
**wrong for this run**: `ba1f3675` r0 ran 4332s (and succeeded), and
`0d4ec3ad` r2 was killed at exactly 5400s. So this arm is precisely "turn-
uncapped, wall-clock-capped at 90 min," and the wall-clock cap cost
`0d4ec3ad` its third repeat. The true turn-unbounded ceiling for `0d4ec3ad`
may be marginally higher than the 0.551 measured here. This does not change
the headline (uncapped ≈ mcn7, and `0d4ec3ad` needs ≫90 turns) but it means
the −0.249 `0d4ec3ad` delta vs mcn7 is partly an artifact of the wall-clock
guard, not a pure turn effect. A follow-up would lift `timeout_seconds` for a
clean turn-only-unbounded measurement.

## Verdict

Removing the turn cap returns the with-sg SDLC family to the mcn7 ceiling
(0.659, statistically indistinguishable from mcn7 with-sg 0.687 and baseline
0.683) and is the only arm to clear the cap=50 collapse with a CI that
excludes 0. The mechanism is now explicit: **`0d4ec3ad` requires 170–180
turns to succeed**, which is why every cap ≤90 truncated it to 0.0. The sweep
curve is flat between 75 and 90 and rises only toward ∞, with the entire gain
attributable to one turn-expensive task.

**Recommendation input for the parent (`codeprobe-4cl6`) A4 default:** there
is no single turn cap that both (a) matches the uncapped ceiling and (b)
bounds cost/latency — 75 captures 4/5 tasks fully and is the cost-efficient
knee, while only an unbounded (or ≥180) budget recovers `0d4ec3ad`. The honest
cross-arm reading is a **task-dependent budget**: cap75 as the family default,
with `0d4ec3ad`-class long-horizon tasks flagged for an uncapped lane rather
than padding the cap for all tasks. Final A4 wording is the parent's call once
this arm is folded into the cross-arm analysis.

## Acceptance check (bead `codeprobe-4cl6.3`)

- [x] 15 trials complete; envelopes live under
      `gascity-mcp-comparison/.codeprobe/runs/with-sg-uncapped/`
- [x] `num_turns` distribution recorded (n=14; median 72.5, range 30–180);
      confirms the ~60–90-turn expectation at the median and exposes the
      170–180-turn tail that the caps were truncating
- [x] Reward statistically indistinguishable from mcn7's with-sg baseline
      (paired-t: Δ = −0.027, 95% CI [−0.195, +0.140], t = −0.45)
- [x] Writeup at `docs/investigations/codeprobe-4cl6/uncapped.md`
- [x] Commit on main; bead close metadata set

## Artifacts

- `docs/investigations/codeprobe-4cl6/per_trial_uncapped.json` — 15 normalized trials
- `docs/investigations/codeprobe-4cl6/per_family_summary_uncapped.json` — rollup + 3 contrasts + turn distribution
- `docs/investigations/codeprobe-4cl6/analyze_uncapped.py` / `analyze_uncapped.out` — deterministic analyzer (ZFC-compliant)
- `docs/investigations/codeprobe-4cl6/logs-sdlc-uncapped/run-20260613-003724.{stdout,stderr}.log` — the single invocation
- `docs/investigations/codeprobe-4cl6/experiment-uncapped.json` — the run config (`max_turns: null`)
- Raw run dir: `/home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe/runs/with-sg-uncapped/`
