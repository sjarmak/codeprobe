# codeprobe-4cl6.2 — with-sg-cap90: SDLC max-turns retune, cap=90 sweep point

**Status:** complete
**Bead:** `codeprobe-4cl6.2` (child of `codeprobe-4cl6`)
**Siblings:** `codeprobe-4cl6.1` (`with-sg-cap75` — `cap75.md`),
`codeprobe-4cl6.3` (`with-sg-uncapped` — pending)
**Predecessor:** `codeprobe-aupz` (`with-sg-fixed`, cap=50)

## Purpose

cap75 (`codeprobe-4cl6.1`) recovered the with-sg SDLC family from 0.155
(cap=50) to 0.507 but left one task (`0d4ec3ad`) deterministically
exhausting the budget at 3/3 cap-hits, and put `45b581b5` on the boundary
(2/3). This sweep point measures whether **90 turns** — the upper end of
mcn7's uncapped 60–90-turn distribution — clears those tasks or whether the
extra headroom buys nothing. Same 5 SDLC tasks × N=3.

## Configuration

`with-sg-cap90` = aupz `with-sg-fixed` (sourcegraph MCP + preamble v2 +
MCP-vs-local guardrail + populated sg_repo + ovz2 oracle_checks/sdlc
branches + riad verify-via-local-Grep) with:

- `max_turns = 90` (cap75 used 75; aupz 50)
- `extra.timeout_seconds = 5400` — same as cap75, so the per-task timeout
  cannot confound the cap measurement. Confirmed not binding: the longest
  trial ran 3,558s.

Run setup otherwise identical to cap75: target
`/home/ds/test_repos/gascity/gascity-mcp-comparison/`, tenant
`codeprobe-4cl6`, model `claude-sonnet-4-6`, `--parallel 2`, suite
`docs/investigations/codeprobe-aupz/suite-sdlc.toml`.

## Run forensics (two invocations, one logical run)

1. **run1** (pid 3478784, launched ~18:44 by another session's babysitter
   loop) — killed at 19:34 along with its launcher and both task workers:
   the whole process group died at once when the launching session ended
   and the harness `killpg`'d the tree (`nohup` alone does not survive
   `killpg`). One trial survived via checkpoint (`0d4ec3ad` r1, reward
   0.8257, $7.02). Two in-flight trials (`0d4ec3ad` r0 ~50min, r2 ~3min)
   died without result artifacts — unrecorded API spend, est. $5–8 wasted.
2. **run2** (pid 2342065, logs `logs-sdlc-cap90/run2.*.log`) — relaunched
   19:37 via `setsid` (own process session, immune to launcher-session
   teardown), `--max-cost-usd 83` (= $90 auth − $7.02 banked). Checkpoint
   resume confirmed ("Skipping 0d4ec3ad repeat 1"). Completed the remaining
   14 trials and banked the 15th (`fde8e6e0` r2) **before** the soft cap
   fired: "Cost budget exceeded: $92.62 > $83.00 — halting" at 00:32 ET, by
   which point the checkpoint already held 15/15. No top-up or third
   invocation needed.

**Cost accounting:** kept 15 trials = $99.64 (run1's $7.02 banked trial +
run2's $92.62). The $92.62 run2 spend overshot its $83 soft cap by $9.62 —
the standard in-flight-parallelism overshoot (the cap is checked between
dispatches, not mid-trial), same behavior as aupz/cap75. run1
quota/process-death waste: ~$5–8 (two artifact-less trials).

**Checkpoint honesty note:** run2 executed pre-`codeprobe-8up` code, so its
5 genuine `error_max_turns` trials are stored `status='error'`. They were
**not** surgically flipped this time — they are kept as valid samples by
reading each error trial's terminal `agent_output.txt` result record
(`subtype=error_max_turns` ⇒ genuine cap-hit; the analyzer's `hit_max_turns`
field is computed from exactly that record, not from the ambiguous
`status`/`error_category` columns). All 5 error rows verified as genuine
cap-hits; zero infra casualties in the final 15.

## Aggregate results

| config | n | mean_reward | cap-hit rate | total_cost | mean_time/trial | total output tok |
|--------|---|-------------|--------------|------------|------------------|------------------|
| mcn7 baseline (no MCP, uncapped) | 15 | 0.6831 | 0% | $69.11 | 1118s | 600,745 |
| mcn7 with-sourcegraph (uncapped) | 15 | 0.6866 | 0% | $85.17 | 1889s | 1,739,453 |
| aupz with-sg-fixed (cap=50) | 15 | 0.1548 | 86.7% | $54.36 | 1363s | 1,251,436 |
| with-sg-cap75 (`4cl6.1`) | 15 | 0.5071 | 26.7% | $84.37 | 2036s | 1,662,179 |
| **with-sg-cap90 (this)** | 15 | **0.5015** | **33.3%** | $99.64 | 2382s | 1,894,350 |

`mean_reward` is the mean of per-task means (each task weighted equally,
not each trial). The paired contrasts below use the same per-task-mean
basis.

## Per-trial detail

| task | repeat | reward | result | num_turns | tool_calls | wall-clock | cost | output tok |
|------|--------|--------|--------|-----------|------------|------------|------|------------|
| ba1f3675 | 0 | 0.0000 | error_max_turns | 91 | 110 | 1881s | $6.47 | 93,373 |
| ba1f3675 | 1 | 0.0000 | error_max_turns | 91 | 126 | 1078s | $5.58 | 48,860 |
| ba1f3675 | 2 | 0.7866 | success | — | 107 | 1341s | $5.36 | 75,785 |
| d906ac3d | 0 | 0.5645 | success | — | 40 | 1842s | $4.16 | 93,526 |
| d906ac3d | 1 | 0.0000 | error_max_turns | 91 | 110 | 3558s | $9.35 | 195,354 |
| d906ac3d | 2 | 0.5650 | success | — | 65 | 3465s | $7.27 | 206,999 |
| 0d4ec3ad | 0 | 0.0000 | error_max_turns | 91 | 163 | 3553s | $11.75 | 145,395 |
| 0d4ec3ad | 1 | 0.8257 | success | — | 99 | 2718s | $7.02 | 131,170 |
| 0d4ec3ad | 2 | 0.0000 | error_max_turns | 91 | 123 | 2032s | $6.99 | 91,640 |
| 45b581b5 | 0 | 0.8313 | success | — | 102 | 2833s | $7.72 | 148,828 |
| 45b581b5 | 1 | 0.6307 | success | — | 56 | 1901s | $4.66 | 108,023 |
| 45b581b5 | 2 | 0.8294 | success | — | 62 | 1695s | $4.42 | 90,826 |
| fde8e6e0 | 0 | 0.8301 | success | — | 71 | 3066s | $6.95 | 181,931 |
| fde8e6e0 | 1 | 0.8297 | success | — | 112 | 2588s | $7.34 | 148,552 |
| fde8e6e0 | 2 | 0.8301 | success | — | 48 | 2185s | $4.61 | 134,088 |

`num_turns` is persisted only for error-terminated trials (the adapter
stores the raw CLI result record on error; successes hold final answer
text). Every cap-hit recorded `num_turns=91` (= cap+1; the terminal result
turn is counted), exactly as cap75 cap-hits recorded 76. `tool_calls` is the
activity proxy for finished trials.

## Max-turns hit rate (the bead's A4-equivalent)

**5/15 = 33.3%** (cap=75 was 4/15 = 26.7%; cap=50 was 13/15 = 86.7%).
Per-task map:

| task | repeat 0 | repeat 1 | repeat 2 |
|------|----------|----------|----------|
| ba1f3675 | hit @91 (r=0.0) | hit @91 (r=0.0) | finished (r=0.787) |
| d906ac3d | finished (r=0.565) | hit @91 (r=0.0) | finished (r=0.565) |
| 0d4ec3ad | hit @91 (r=0.0) | finished (r=0.826) | hit @91 (r=0.0) |
| 45b581b5 | finished (r=0.831) | finished (r=0.631) | finished (r=0.829) |
| fde8e6e0 | finished (r=0.830) | finished (r=0.830) | finished (r=0.830) |

As at cap=50 and cap=75, **every cap-hit scored 0.0** — mid-edit
termination never yields a scoreable change set. Reward stays bimodal in
the cap: a task either fits the budget and scores at its natural level, or
it doesn't and scores 0.

### Cap-hit count is non-monotonic in the cap — and that is sampling noise

cap90 hit the cap *more* than cap75 (5 vs 4), which is counterintuitive:
more turns should mean fewer forced terminations, not more. The per-task
movement vs cap75 is the tell:

- `45b581b5`: 2/3 → **3/3 finished** (cleared, as cap75.md predicted).
- `0d4ec3ad`: 3/3 hit → 2/3 hit (one sample now finishes at 0.826).
- `ba1f3675`: **3/3 finished → 1/3 finished** (apparent regression).
- `d906ac3d`: 3/3 finished → 2/3 finished (apparent regression).

These runs are **independent stochastic samples** — the agent is
`claude-sonnet-4-6`, not deterministic, and cap75 and cap90 were separate
runs with effectively different seeds. A task finishing 3/3 at cap75 and
1/3 at cap90 is not evidence that 90 turns *causes* failures `ba1f3675`
avoided at 75; it is N=3 sampling variance on a bimodal outcome. The
honest reading is that **per-task cap-hit counts at N=3 are too noisy to
rank caps by**, and the paired reward contrast (below, Δ = −0.006,
indistinguishable) is the load-bearing statistic, not the raw hit count.

## Paired contrasts (per-task means, df=4, t_crit=2.776)

> The cap75 contrast is computed from the live `with-sg-cap75` run dir,
> which the analyzer's provenance filter trims to 14 trials / 3 cap-hits
> (it drops one early-run residue trial flagged by `CAP75_RERUN_EPOCH`).
> Its per-task means — and therefore mean_reward 0.5071 — match the
> canonical 15-trial `cap75.md` rollup, so the paired delta is unaffected;
> only the raw cap75 hit *count* differs (3 here vs the canonical 4/15).

### cap90 vs cap75 (primary)

**mean Δ = −0.0055**, 95% CI **[−0.436, +0.425]**, t = −0.036 →
**statistically indistinguishable**. The extra 15 turns produce no reward
change. Per-task deltas: `ba1f3675` −0.525, `d906ac3d` −0.188, `0d4ec3ad`
+0.275, `45b581b5` +0.277, `fde8e6e0` +0.133 — they cancel almost exactly,
which is the signature of two independent samples of the same underlying
distribution, not a real effect of the cap.

### cap90 vs aupz cap50

**mean Δ = +0.3467**, 95% CI **[−0.112, +0.806]**, t = 2.10 → large
positive recovery (same shape as cap75's +0.352 vs cap50), CI just includes
0 at n=5. cap90, like cap75, lifts the family well above the cap=50
collapse.

### cap90 vs mcn7 baseline (parent A3 reference)

**mean Δ = −0.1816**, 95% CI **[−0.498, +0.135]**, t = −1.59 → CI contains
0, so cap=90 formally meets the parent's A3 criterion (paired CI vs mcn7
baseline overlaps 0). As with cap75 this is partly low power: `0d4ec3ad`
still loses ~0.52 vs baseline (2/3 cap-hit), partially offset by `fde8e6e0`
(+0.15).

### cap90 vs mcn7 with-sourcegraph (uncapped)

**mean Δ = −0.1850**, 95% CI **[−0.500, +0.130]**, t = −1.63 → same shape
as the baseline contrast (mcn7's two arms scored near-identically). cap=90
reward trends below the uncapped ceiling but is not separable at N=5.

## Observations

1. **Raising the cap 75 → 90 buys cost, not reward.** Mean reward is
   statistically identical (0.5071 → 0.5015, Δ = −0.006), while cost rose
   +18% ($84.37 → $99.64), mean wall-clock +17% (2036s → 2382s), and total
   output tokens +14%. The extra headroom is spent, not converted to score.
2. **`0d4ec3ad` is the structural ceiling and 90 turns does not lift it.**
   2/3 trials still burned the full budget (91 turns, 123–163 tool calls,
   $6.99–$11.75 each for 0 reward); only one sample finished (0.826).
   mcn7's uncapped with-sg scored 0.80 on it, so it is solvable — the
   `with-sg-uncapped` arm (`4cl6.3`) is required to find where, if anywhere,
   it clears.
3. **`45b581b5` cleared, as cap75.md predicted** — 3/3 finished at
   0.83/0.63/0.83 (was 2/3 at cap75, 0/3 at cap50). This is the one task
   where the larger cap demonstrably helped across all repeats.
4. **Capped failures remain expensive failures.** The 5 cap-hits cost
   $40.14 for 0 reward — 40% of the run's spend bought nothing. Higher caps
   make each such failure costlier (cap90 cap-hits averaged $8.03 vs cap75's
   $6.12) without changing the outcome.

## Verdict

cap=90 does **not** improve on cap=75: identical reward (Δ = −0.006, CI
straddles 0), higher cost, slower, and — within sampling noise — no fewer
cap-hits. Both caps satisfy the parent's A3 CI-overlap criterion against
mcn7 baseline, and both leave `0d4ec3ad` structurally unsolved (it exhausts
50, 75, and 90 turns alike). The sweep shows **diminishing returns past
75**: between 75 and 90 the curve is flat, so 90 is not the recommended
default — if anything cap75 dominates it on cost. Whether the SDLC family
needs an *unbounded* budget to recover `0d4ec3ad`, or whether that task is
intractable for this with-sg config regardless of cap, is the question the
uncapped control (`codeprobe-4cl6.3`) answers. The parent's A4 default
recommendation stays deferred to the cross-arm analysis once the uncapped
arm lands.

## Acceptance check (bead `codeprobe-4cl6.2`)

- [x] 15 trials complete; envelopes live under
      `gascity-mcp-comparison/.codeprobe/runs/with-sg-cap90/` and the
      checkpoint holds 15/15
- [x] Per-trial `error_max_turns` rate computed (33.3%); pair-test vs cap75
      (Δ = −0.006, CI [−0.436, +0.425]) and aupz `with-sg-fixed` cap=50
      (Δ = +0.347, CI [−0.112, +0.806])
- [x] Writeup at `docs/investigations/codeprobe-4cl6/cap90.md`
- [x] Commit on main; bead close metadata set

## Artifacts

- `docs/investigations/codeprobe-4cl6/per_trial_cap90.json` — 15 normalized trials
- `docs/investigations/codeprobe-4cl6/per_family_summary_cap90.json` — rollup + 4 contrasts
- `docs/investigations/codeprobe-4cl6/analyze_cap90.py` / `analyze_cap90.out` — deterministic analyzer (ZFC-compliant)
- `docs/investigations/codeprobe-4cl6/logs-sdlc-cap90/run2.{stdout,stderr}.log` — the surviving invocation
- Raw run dir: `/home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe/runs/with-sg-cap90/`
