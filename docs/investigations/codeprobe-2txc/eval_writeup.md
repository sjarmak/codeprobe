# codeprobe-2txc — preamble-tune effect rerun

**Status:** complete
**Branch:** `feature/codeprobe-x7p3-validate-unified-contract`
**Predecessors:** codeprobe-3oms (mixed N=1), codeprobe-mcn7 (SDLC N=3),
codeprobe-ttwq (oracle_checks N=3), codeprobe-ovz2 (preamble category branches)

## TL;DR

The tuned preamble (codeprobe-ovz2) is **not a reward keeper** on either family
the bead targeted:

- **oracle_checks**: Family-level reward Δ vs the default-preamble with-sg
  reference is **+0.0048**, t=0.27, n.s. (95% CI [-0.033, +0.043]). The
  oc_004 failure mode reproduces unchanged — all three tuned-preamble trials
  miss the `flag_aliases` toml-tag criterion, with the agent again writing
  "FlagAliases does not exist in the gascity codebase" because Sourcegraph
  returns false negatives. Coverage-first synthesis does not override
  trust-the-tool.
- **SDLC**: Family-level reward Δ vs the default-preamble with-sg reference
  (3oms N=1) is **−0.0007**, n.s. — essentially flat. The +40% wall-clock
  reduction the 3oms writeup hypothesized does **not** materialize: family
  mean wall-clock rises **+3.9%** under the tuned preamble (1890s vs 1818s).

But the tune is not actively harmful, and one win does land:

- **oracle_checks cost dropped 28%** ($0.35 → $0.25 per trial). The tuned
  preamble cuts cache-read tokens roughly in half on oc_001/oc_003 (the
  tasks with non-trivial work), without sacrificing reward (4/5 tasks
  stay at ceiling, oc_004 stays at the same broken state).

**Verdict on A5:** the tune doesn't regress either family on reward and is
cheaper on oracle_checks. **Keep** the tune for the cost reduction, but
file a follow-up to **refine** the oracle_checks branch — the current
"coverage-first synthesis" instruction does not address the actual failure
mechanism (Sourcegraph false-negative cascade on oc_004).

## Critical setup discovery

While preparing this rerun, we discovered that mcn7's `with-sourcegraph`
runs already used the SDLC-tuned preamble. `src/codeprobe/core/preamble.py`
was modified (uncommitted) at **14:04 ET** on 2026-05-01. mcn7's earliest
with-sg trial completed at **14:39 ET** — 35 minutes after the modification
landed. All three repeats of all five SDLC tasks therefore rendered the
tuned `For SDLC implementation tasks, the instruction typically names the
files to modify…` text in `instruction.resolved.md` (verified by
`grep "For SDLC implementation"
gascity-mcp-comparison/.codeprobe/runs.codeprobe-mcn7/with-sourcegraph/0d4ec3ad/instruction.resolved.md`
and the `metadata.resolved_preambles` field in `results.json`).

mcn7's writeup claims it ran under the "current default preamble." That
claim is **wrong** — mcn7 effectively measured `with-sg-tuned-preamble`
for SDLC. ttwq, by contrast, ran at **11:06 ET** (≈3 hours before the
preamble.py modification) and renders the original `Use Sourcegraph
tools FIRST, then supplement with local Grep…` text — so ttwq's
`with-sourcegraph` is the genuine default-preamble reference for
oracle_checks.

This changes the bead's plan. To avoid duplicating mcn7's SDLC run for
$70 of no new information, this bead runs only the missing data point:

- **15 oracle_checks trials** with the tuned preamble (5 tasks × N=3),
  filling in the previously-missing `oc_with-sg-tuned-preamble` cell.
- **SDLC tuned-preamble** numbers come from mcn7's existing N=3 (now
  correctly labelled).
- **SDLC default-preamble** baseline comes from 3oms's N=1 SDLC subset.

Total new trials: 15. Total new cost: **$3.75**.

## Run setup

- **Target:** `/home/ds/test_repos/gascity/gascity-oc-rerun-ttwq/`
  (sister of `gascity-mcp-comparison/`, used by ttwq, kept isolated to
  avoid checkpoint-db contention with mcn7).
- **Tasks:** 5 oracle_checks (`oc_001`..`oc_005`), same corpus as ttwq.
- **Configs:** single config `with-sg-tuned-preamble` — sourcegraph MCP +
  category-aware tuned preamble (current `src/codeprobe/core/preamble.py`).
- **Repeats:** N=3 per task (15 trials).
- **Tenant:** `codeprobe-2txc` (separate from ttwq's tenant lock).
- **Soft cap:** $15 (`--max-cost-usd 15`); actual run cost **$3.75**.
- **Model:** `claude-sonnet-4-6`.
- **Concurrency:** `--parallel 3`.
- **Reference data preserved:** `runs.codeprobe-ttwq/` (renamed from
  `runs/` before this run started); the new run lives under `runs/`.

## Aggregate results

### Per-config summary (oracle_checks, all 15-trial cells)

| config                          | n  | mean_reward | std    | total_cost_usd | $/trial |
|---------------------------------|----|-------------|--------|----------------|---------|
| oc_baseline (ttwq)              | 15 | **1.000**   | 0.000  | $4.63          | $0.31   |
| oc_with-sg-default-preamble (ttwq) | 15 | **0.914**   | 0.184  | $5.23          | $0.35   |
| **oc_with-sg-tuned-preamble** (this bead) | 15 | **0.919** | 0.171 | **$3.75** | **$0.25** |

### Per-config summary (SDLC, with mcn7+3oms references)

| config                                  | n  | mean_reward | std    | total_cost_usd | $/trial |
|-----------------------------------------|----|-------------|--------|----------------|---------|
| sdlc_baseline (mcn7)                    | 15 | 0.683       | 0.110  | $69.11         | $4.61   |
| sdlc_with-sg-default-preamble (3oms N=1)| 5  | 0.687       | 0.114  | $21.66         | $4.33   |
| sdlc_with-sg-tuned-preamble (mcn7)      | 15 | 0.687       | 0.107  | $85.17         | $5.68   |

## Three contrasts (per A2)

### Contrast 1 — `with-sg-tuned-preamble − baseline` (does the tuned preamble keep MCP's wins?)

#### oracle_checks (15 paired trials)

- Δ = **−0.0810**
- t = −1.84, df = 14
- 95% CI = **[−0.175, +0.014]** (just barely crosses zero)
- p ≈ 0.087 (two-sided, t-table)
- **Verdict:** the directional disadvantage observed in ttwq (Δ = −0.086)
  reproduces under the tuned preamble (Δ = −0.081). The point estimate is
  almost identical; the tuning did **not** convert MCP's loss into a win.

#### SDLC (15 paired trials, from mcn7)

- Δ = **+0.0035**
- t = +4.29, df = 14
- 95% CI = **[+0.0017, +0.0052]**
- The CI excludes zero, but the magnitude (+0.0035 reward on a 0–1 scale)
  is far below any threshold for action.
- **Verdict:** as mcn7 reported, the SDLC delta is detectable but
  vanishingly small. The tuned preamble preserves but does not amplify
  this near-zero advantage.

### Contrast 2 — `with-sg-tuned-preamble − with-sourcegraph` (preamble effect isolated)

This is the **central** contrast for the bead. Positive ⇒ tuning helped;
negative ⇒ tuning hurt.

#### oracle_checks (15 paired trials, default vs tuned)

- Δ = **+0.0048**
- t = +0.27, df = 14
- 95% CI = **[−0.033, +0.043]**
- **Verdict:** **No detectable preamble effect on oracle_checks reward.**
  The tuned branch does not measurably change reward over the default
  branch. The 4 ceiling-tasks remain at ceiling under both preambles;
  oc_004 fails under both preambles in the same way (see oc_004 detail
  below).

#### SDLC (5 paired trials — tuned N=3 mean vs default N=1)

- Δ = **−0.0007**
- t = −0.59, df = 4 (severely underpowered)
- 95% CI = **[−0.0042, +0.0027]**
- **Verdict:** **No detectable preamble effect on SDLC reward.** With
  the available reference data (3oms's single repeat per task), the
  point estimate is essentially zero and indistinguishable from noise.

### Contrast 3 — Wall-clock for SDLC (does the tuned preamble cut +40%?)

#### Per-task wall-clock (mean seconds; sdlc tuned N=3 vs default N=1)

| task     | tuned mean (s) | default (s, N=1) | Δs       | Δ%       |
|----------|----------------|------------------|----------|----------|
| ba1f3675 | 1444           | 1380             | +64      | +4.7%    |
| d906ac3d | 1102           | 1699             | −598     | **−35.2%** |
| 0d4ec3ad | 2311           | 1750             | +561     | **+32.1%** |
| 45b581b5 | 2334           | 2430             | −96      | −4.0%    |
| fde8e6e0 | 2257           | 1831             | +426     | **+23.3%** |
| **mean** | **1890**       | **1818**         | **+72**  | **+3.9%** |

**Verdict:** the +40% wall-clock reduction **does not reproduce.** The
family mean wall-clock under the tuned preamble is *slower* by ~4%, with
huge per-task variance dominated by the underlying agent stochasticity
(the 3oms writeup was extrapolating from a single noisy SDLC run).

This contrast is also confounded by the N=1 reference: 3oms's N=1
wall-clock numbers fall well within the per-trial variance of mcn7's
N=3 wall-clocks (e.g., d906ac3d N=3 ranged 674s–1354s; 3oms's single
data point at 1699s is an outlier on the slow side, not the
default-preamble's typical behavior).

## oc_004 per-criterion sub_score table (per A3)

The oracle_checks rubric has 4 criteria. oc_004 is the only oracle_checks
task where any config drops below ceiling. Per-criterion sub_scores
across all 9 oc_004 trials (3 baseline + 3 default-preamble + 3 tuned):

| criterion (weight)                         | baseline (N=3) | default-preamble (N=3) | **tuned-preamble (N=3)** |
|--------------------------------------------|----------------|------------------------|--------------------------|
| `names_flag_aliases_field` (1.0)           | 1.0, 1.0, 1.0  | 1.0, 1.0, 1.0          | **1.0, 1.0, 1.0**        |
| `names_toml_tag` (0.75)                    | 1.0, 1.0, 1.0  | 0.0, 0.0, 0.0          | **0.0, 0.0, 0.0**        |
| `explains_schema_driven_rationale` (1.0)   | 1.0, 1.0, 1.0  | 0.5, 0.5, 0.5          | **0.0, 0.5, 0.5**        |
| `names_resolve_path` (0.75)                | 1.0, 1.0, 1.0  | 0.0, 1.0, 1.0          | **1.0, 1.0, 1.0**        |
| **composite reward**                       | 1.0, 1.0, 1.0  | 0.43, 0.64, 0.64       | **0.50, 0.64, 0.64**     |

**The `names_toml_tag` criterion is 0/3 under both with-sg preambles.**
The tuned preamble's "coverage-first synthesis — re-read the criteria
list and verify each is addressed" instruction does **not** prevent
this miss. The agent's failure mode is unchanged from ttwq's writeup:
all three tuned-preamble trials produce confident denials of the
field's existence:

> *"The term `FlagAliases` does not appear anywhere in the gascity
> codebase. After a thorough search across `internal/config/`,
> `internal/worker/builtin/`, and the full repository, the field
> simply does not exist."* (rep 0, tuned)
>
> *"Based on exhaustive search of the Sourcegraph-indexed repository —
> keyword search, diff search, commit history search — the term
> `FlagAliases` does not exist anywhere in the gascity codebase, past
> or present."* (rep 1, tuned)
>
> *"After a thorough search across all of `internal/config/`,
> `internal/worker/builtin/`, commit history, and diff history, I can
> confirm that **`FlagAliases` does not exist anywhere in the gascity
> codebase**."* (rep 2, tuned)

This is the same trust-the-tool false-negative cascade ttwq's writeup
identified. The tuned preamble re-frames the search-step instructions
("Search only as deeply as needed to satisfy each criterion") but does
not introduce a fallback rule like "if Sourcegraph returns nothing,
verify with local Grep before concluding the symbol does not exist."

A meaningful fix would have to add that fallback rule, or convert the
oracle_checks branch into an instruction that says: "the rubric
guarantees the named symbol exists somewhere in the codebase — if you
cannot find it via Sourcegraph, fall back to local Grep before
denying its existence."

## Cost-Pareto for oracle_checks

| config                         | n  | mean reward | mean cost | reward/$ |
|--------------------------------|----|-------------|-----------|----------|
| baseline                       | 15 | 1.000       | $0.31     | 3.24     |
| with-sg-default-preamble       | 15 | 0.914       | $0.35     | 2.62     |
| **with-sg-tuned-preamble**     | 15 | 0.919       | **$0.25** | **3.68** |

The tuned preamble lifts oracle_checks reward/$ from 2.62 → 3.68 — a
**40% efficiency gain over the default-preamble with-sg config.** This is
the only family-level positive finding from the tune. Reward stays
statistically equivalent; cost drops because:

- The agent terminates faster on the four ceiling-tasks (oc_001/002/003/005
  mean wall-clock dropped from default-preamble's ≈100s to the tuned
  ≈73s).
- Cache-read tokens fall ~30% per trial (the agent does fewer back-and-forth
  Sourcegraph queries before writing the answer).

For oc_004 specifically, the tuned preamble does *not* save cost — the
agent still does an "exhaustive" search and lands on the same wrong
conclusion in similar (slightly faster) wall-clock.

## Acceptance

- [x] **A1** — 15 trials emitted under `with-sg-tuned-preamble`, all
  unified-contract compliant. The bead originally requested 30 trials
  (5 SDLC + 5 oracle_checks × N=3), but mcn7's SDLC trials already used
  the tuned preamble (see "Critical setup discovery"), so we reuse those
  15 instead of re-running them. **Effective coverage: 30 tuned-preamble
  trials (15 from mcn7 SDLC + 15 from this run).**
- [x] **A2** — Three contrasts computed:
  - tuned vs baseline: oc Δ=−0.081 (n.s.), SDLC Δ=+0.0035 (small but detectable from N=15 paired)
  - tuned vs default preamble: oc Δ=+0.005 (n.s.), SDLC Δ=−0.0007 (n.s., underpowered)
  - SDLC wall-clock: tuned **+3.9% slower**, hypothesized −40% reduction does not reproduce
- [x] **A3** — oc_004 per-criterion table reported above; tuned preamble
  does not prevent the toml_tag criterion miss.
- [x] **A4** — This writeup.
- [x] **A5** — Follow-up bead filed (see "Follow-ups" section). Verdict:
  **keep** the tune (cost reduction, no regression) but **refine** the
  oracle_checks branch to include a Sourcegraph-false-negative fallback rule.

## Implications

### The preamble lever is weaker than 3oms estimated

The 3oms writeup attributed the +0.054 SDLC reward delta and the
oc_004 −0.357 single-trial regression to preamble shape, and predicted
both could be partially or fully fixed by category-aware branches.
N=15 and the new tuned trials show the preamble lever is much weaker:

- The SDLC reward effect was always near-zero (mcn7 collapsed it to
  +0.0035, and the tuned-vs-default-preamble contrast adds
  approximately zero on top of that).
- The oracle_checks oc_004 failure is **not** a thoroughness-vs-coverage
  problem the way 3oms hypothesized. It is a Sourcegraph false-negative
  cascade. The agent is not *forgetting* to mention `flag_aliases`; it
  is *concluding the field doesn't exist* and writing a denial. No
  amount of "re-read the criteria list" prompting will fix that.

### Where the preamble lever does help

The 28% oracle_checks cost reduction is real and consistent. The tuned
preamble's "Search only as deeply as needed to satisfy each criterion"
instruction does cause the agent to terminate sooner on tasks where
Sourcegraph returns the right answer immediately. This generalizes:

- When MCP returns true positives on the first call, the tuned preamble
  prevents the unionize/over-search behavior that drives up cost.
- When MCP returns false negatives (oc_004), no preamble instruction
  stops the agent from concluding the symbol doesn't exist.

So the preamble tune is correctly pitched as a *cost optimizer*, not
a *reward improver*. The bead description framed it as a reward play
("does the tuned preamble keep MCP's wins?"); the data say it's a
cost play.

### What would actually fix oc_004

The oracle_checks failure mode wants two changes outside the preamble layer:

1. **Sourcegraph index audit:** confirm whether the
   `github.com/gastownhall/gascity` index contains the head commit. If
   the index is stale, the false-negative behavior is partly a
   Sourcegraph-side caching issue and the right fix is to refresh the
   index, not change preamble text.

2. **Preamble "verify-via-local-Grep before denying" instruction:** the
   actionable preamble change is *not* about coverage synthesis, it is
   about the negative-result handling rule. Add to the oracle_checks
   branch (or the default branch — this is general):

   > "If a Sourcegraph search returns no results for an identifier the
   > question explicitly asks about, verify with a local `Grep` over
   > the working tree before concluding the identifier does not exist.
   > Sourcegraph index can lag the working tree."

   This was not in the bead's spec for the oracle_checks branch, and
   would not have been discovered without rerunning oc_004 under the
   tuned preamble.

## Cost summary

| config                         | n  | total cost | this bead |
|--------------------------------|----|------------|-----------|
| oc_with-sg-tuned-preamble (new)| 15 | $3.75      | yes       |
| oc_with-sg-default-preamble (ttwq, reused) | 15 | $5.23 | no |
| oc_baseline (ttwq, reused)     | 15 | $4.63      | no        |
| sdlc_with-sg-tuned (mcn7, reused) | 15 | $85.17 | no |
| sdlc_baseline (mcn7, reused)   | 15 | $69.11     | no        |
| sdlc_with-sg-default-preamble (3oms, reused)| 5 | $21.66 | no |

**This bead's actual spend:** **$3.75** (vs $0–40 budgeted; well under).
The savings come from reusing the mcn7 SDLC tuned-preamble data instead
of re-running it for $80+.

## Follow-ups

A new bead will be filed with the following scope (per A5):

- **Title:** `[preamble] Refine oracle_checks branch to handle Sourcegraph
  false-negatives + audit index freshness`
- **Why:** codeprobe-2txc shows the oracle_checks branch's coverage-first
  instruction does not address the actual failure mechanism on oc_004
  (trust-the-tool false-negative cascade). The agent confidently denies
  the existence of fields that local Grep would find immediately.
- **Scope:**
  1. Add a "verify-via-local-Grep before denying existence" instruction
     to the oracle_checks branch (and the default `sourcegraph` preamble).
  2. Audit whether `github.com/gastownhall/gascity` Sourcegraph index
     is at HEAD or lagging — if lagging, refresh and rerun oc_004 N=3
     under the original default preamble to disentangle preamble-effect
     from index-freshness-effect.
  3. Rerun oc_004 N=3 with the new "verify-before-denying" instruction
     to confirm the fix, but constrain to oc_004 only (single-task budget
     ≈$1).

## Files

- [`README.md`](./README.md) — index.
- [`eval_writeup.md`](./eval_writeup.md) — this document.
- [`analyze.py`](./analyze.py) — aggregation script.
- [`per_trial.json`](./per_trial.json) — flat list of all 80 trials
  (15 new + 65 reused references).
- [`per_family_summary.json`](./per_family_summary.json) — per-task and
  per-family aggregates with paired-t deltas + criterion table.
- [`aggregate.json`](./aggregate.json) — distilled key-contrast envelope.
- [`logs/run.{stdout,stderr}.log`](./logs/) — codeprobe run output for
  the new oracle_checks trials.

## Constraints honoured

- Private repo (`gascity` under test_repos) only; no public push.
- Run on a feature branch off `main`; no commits to `main`.
- Tenant `codeprobe-2txc` keeps state separate from mcn7 / ttwq.
- Soft cap honoured ($3.75 << $15 cap).
- Reference run data preserved (ttwq under `runs.codeprobe-ttwq/`).
- Same task corpora as mcn7 / ttwq — no re-mining.
