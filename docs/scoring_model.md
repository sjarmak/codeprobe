# Scoring model — reward vs IR diagnostics

> codeprobe-voxa (2026-04-29). Pairs with the multi-backend oracle curator
> (codeprobe-zat9). Re-run validator: `codeprobe-3ljz`.

## TL;DR

For information-retrieval (IR) tasks — `file_list`, `symbol_list`, the
legacy file-list, and the org-scale weighted oracle — the **headline
reward is recall**, not F1. Precision and F1 are still computed and
reported, but they live in an **IR diagnostics** view alongside the
reward; they no longer suppress the score when an agent over-ships.

| Concept            | Question it answers                                     | Where to find it                                         |
| ------------------ | ------------------------------------------------------- | -------------------------------------------------------- |
| **Reward**         | Did the agent find what the oracle expected?            | `ScoreResult.score` / `ScoreResult.reward_score` / `mean_automated_score` (alias `mean_reward`) |
| **IR diagnostics** | How clean was the answer? Over-ship vs miss vs balanced | `ScoreResult.ir_metrics` / `ir_diagnostics.{mean_precision,mean_recall,mean_f1}` |

## Why split them

Before codeprobe-voxa, IR scorers returned `score = F1`. F1 punishes
over-shipping the same way it punishes missing the answer — a fast,
imprecise tool that returns "every file in the repo, including all 80
the oracle wanted" was scored 0.04 on a real task we re-ran (e5d7a4e7),
even though it had recall = 1.0 (it found everything).

That conflated two distinct things:

1. **Did the agent solve the task?** → oracle-matching → reward
2. **How tight was the answer?** → IR shape → diagnostics

A tool boundary (this agent doesn't have a precise index) showed up in
the score table as a quality gap, which is misleading. Splitting reward
from IR diagnostics fixes that without losing any information.

## The reward formula

```
reward = weighted_recall  if oracle has tier weights and emits weighted_recall
       = recall           otherwise
```

Both values live in `[0.0, 1.0]`. The reward is computed mechanically
from set overlap — no thresholds, no soft-clipping, no judgment. Pure
arithmetic on `expected ∩ actual / |expected|` (or its weighted
counterpart for tiered oracles).

### Why recall, not F1 or thresholded F1

We considered three formulas during the codeprobe-voxa design:

* **A. recall-only** — over-shipping is free.
* **B. thresholded F1** — soft-clip F1 to `[0, 1]` with a piecewise
  ramp.
* **C. containment** — recall, but zeroed out when precision drops
  below some floor.

Option **A (recall-only)** was chosen because:

* It matches the user-stated framing literally: "the reward should be
  related to oracle matching".
* B and C smuggle precision back into the reward — exactly the bug the
  bead was filed to fix.
* Pathological "agent dumps the entire repo" is visible via low
  precision in IR diagnostics. Reviewers can see it; it just doesn't
  pollute the reward.
* Simplest formula → fewest hidden parameters → fewest places for ZFC
  drift.

The formula lives in a single helper (`ContinuousScorer._derive_reward_and_metrics`,
plus the inline IR scorers in `core/scoring.py`) and is trivial to swap
if a future task family wants a different shape — e.g. per-task-type
overrides could ride on `answer_type`.

## What gets emitted

### `ScoreResult`

```python
@dataclass(frozen=True)
class ScoreResult:
    score: float                # = reward (unchanged contract field)
    passed: bool                # reward >= PASS_THRESHOLD
    error: str | None = None
    details: dict = ...         # back-compat: still carries precision/recall/f1
    reward_score: float | None  # explicit reward field (mirrors `score`)
    ir_metrics: dict            # canonical IR view: {precision, recall, f1, weighted_recall?}
```

`details` continues to carry `precision`/`recall`/`f1` so older code
that reads `scoring_details["f1"]` keeps working. New code should treat
`ir_metrics` as the canonical source.

### `aggregate.json`

```jsonc
{
  "config_summaries": {
    "baseline": {
      "tasks_completed": 80,
      "mean_automated_score": 1.0,   // headline reward
      "mean_reward": 1.0,            // alias for clarity
      "stdev_automated_score": 0.0,
      "total_cost_usd": 4.20,
      "mean_cost_per_task": 0.05,
      "score_per_dollar": 20.0,
      // back-compat: kept at top level for older consumers
      "mean_precision": 0.26,
      "mean_recall":    1.0,
      "mean_f1":        0.41,
      // canonical IR view going forward
      "ir_diagnostics": {
        "mean_precision": 0.26,
        "mean_recall":    1.0,
        "mean_f1":        0.41
      }
    }
  }
}
```

The flat `mean_precision` / `mean_recall` / `mean_f1` are kept at the
top level so existing dashboards and CI gates don't break. New code
should read them from `ir_diagnostics`.

## Bias detection — informational over-shipping

`detect_overshipping_anti_pattern` no longer triggers on a low-score /
high-recall pair (because the reward is now recall, the loser-vs-winner
score gap collapses for an over-ship case). It now fires informationally
when:

* both configs achieved recall ≥ 0.95 on the same task, AND
* one config's precision ≤ 0.5, AND
* the precision delta between the two ≥ 0.3.

The warning message states explicitly that the **reward is unaffected**
— it surfaces a *behavioural* difference (one config dumps many extra
files; the other ships a tight answer) without implying a quality gap.

## Bias detection — backend_overlap severity

> codeprobe-9re9 (2026-04-29). Pairs with codeprobe-zat9 (multi-backend
> oracle curator).

Every `BiasWarning` carries a `severity` field — `"warning"` or
`"informational"`. For `backend_overlap`, severity is computed from the
relationship between the GT-producing backends and the agent's MCP
surface for the *affected tasks*:

```
gt_backends   = ⋃ task_gt_backends[t]   for t in affected
cfg_backends  = config's MCP server names (sourcegraph variants matched fuzzily)
overlap       = gt_backends ∩ cfg_backends
independent   = gt_backends − cfg_backends
```

* `overlap == ∅`             → no warning at all.
* `independent == ∅`         → severity = `"warning"`. The agent's tool
                                surface fully covers the answer key —
                                the score may be tautological.
* `independent != ∅`         → severity = `"informational"`. The
                                multi-backend curator independently
                                corroborated GT via a backend the agent
                                cannot reach, so the overlap is honest
                                signal rather than measurement bias.

### Where severity surfaces

* **`aggregate.json#bias_warnings[].severity`** — every warning record.
* **`aggregate.json#bias_warnings[].detail.independent_backends`** — for
  `backend_overlap`, the GT backends the config can't reach. Empty when
  severity is `"warning"`.
* **CLI `experiment aggregate`** — actionable warnings render under
  `Bias warnings:`; informational warnings render under a separate
  `Informational:` section so the warnings panel only highlights real
  measurement bias.
* **`quality_metrics.flag_counts`** — the per-trial flag is
  `backend_overlap` for warning severity (back-compat) and
  `backend_overlap_informational` for the informational variant. A
  dashboard filtering for true tautology risks reads `backend_overlap`;
  the informational stream lives under its own key.

### Motivating example

The 2026-04-29 gascity validation run (5 tasks, baseline vs.
with-sourcegraph) emitted a `backend_overlap` warning on every task.
After codeprobe-zat9 the curator records `oracle_backends_consensus =
["ast", "grep", "sourcegraph"]` for each task. The with-sourcegraph
config has only `sourcegraph` in its MCP surface, so `independent =
{ast, grep}` ≠ ∅ — the curator has independent corroboration of every
file in the answer key. Under the severity gate the five warnings are
re-classified `informational`, the `Bias warnings:` panel goes silent,
and the run reads as a clean comparison.

## Reading scoring outputs in practice

* **Ranking configs?** Use `mean_reward` (or its alias
  `mean_automated_score`).
* **Diagnosing why a config wins or loses?** Look at
  `ir_diagnostics.mean_precision` vs `ir_diagnostics.mean_recall`. A
  high-recall / low-precision config solves the task but ships noise; a
  high-precision / low-recall config gives clean answers that miss
  things; a balanced config wins on both.
* **Auditing a single task?** `scoring_details.precision` /
  `scoring_details.recall` / `scoring_details.f1` per task in
  `<exp>/results/<config>/completed.json` (or read `ir_metrics` directly
  off the `ScoreResult`).
* **Investigating an over-shipping pattern?** Look for `overshipping`
  warnings in `aggregate.json#bias_warnings` — they include the
  per-config precision split and the affected `task_id`.

## Worked example: before/after voxa on gascity

> codeprobe-ur8d (2026-04-29). N=3 repeat re-run of the codeprobe-3ljz
> validator. 5 tasks × 2 configs (baseline vs. with-sourcegraph) ×
> 3 repeats = 30 trials, $48.46 total.

The same 5 gascity tasks, scored under three reward formulas, demonstrate
why the voxa pivot is doing real work. `mean_delta` is `with-sg − baseline`:

| Run            | Reward formula | GT source        | mean_delta | Cohen's d | wins (B/SG/tie) |
| -------------- | -------------- | ---------------- | ---------: | --------: | --------------- |
| `wo7n` (old)   | F1             | sg-only          |    +0.265  |   +0.40   | 1 / 3 / 1       |
| `gcwk` (mid)   | F1             | multi-backend GT |    +0.141  |   +0.74   | 1 / 3 / 1       |
| `ur8d` (this)  | **recall**     | multi-backend GT |  **−0.211**| **−0.975**| **3 / 0 / 2**   |

Same agent, same tasks, same MCP surface. Only the reward formula and
oracle backends changed. The headline conclusion flipped sign — and the
per-task numbers explain why:

| Task        | baseline (recall, mean ±sd, N=3) | with-sg (recall, mean ±sd, N=3) | with-sg precision | delta  |
| ----------- | -------------------------------: | ------------------------------: | ----------------: | -----: |
| 38223444    | 0.833 ±0.167                     | 0.333 ±0.000                    | 1.00              | −0.500 |
| 6cf61fea    | 0.889 ±0.096                     | 0.667 ±0.000                    | ≈0.21             | −0.222 |
| b826fa9d    | 0.667 ±0.000                     | 0.667 ±0.000                    | 1.00              |  0.000 |
| d9fee4ae    | 0.833 ±0.000                     | 0.833 ±0.000                    | ≈0.31             |  0.000 |
| e5d7a4e7    | 0.944 ±0.096                     | 0.611 ±0.096                    | ≈0.22             | −0.333 |

**Within-task variance is near zero.** with-sg's stdev is exactly 0.0 on 4
of 5 tasks; baseline tops out at 0.167. The N=3 repeats confirm that
recall is highly stable per `(config, task)` — the gap between runs is
between-task heterogeneity, not within-run noise. (The 95% CI on the
per-task delta of means is `[−0.48, +0.06]` — it straddles zero only
because n=5 tasks, not because the per-trial signal is noisy.)

**Why F1 said the opposite story.** Look at 38223444 with-sg: the agent
writes 2 files, both correct (precision = 1.0, recall = 0.333). F1 = 0.5.
Baseline writes ~16–22 files, finds 4–6 of the 6 correct (precision ≈
0.30, recall ≈ 0.83). F1 ≈ 0.43. Under F1, with-sg wins because its
answer is tighter — even though it found fewer of the files the user
asked for. Under recall, baseline wins because it actually solved the
task. F1 was rewarding tool-shape, not task completion.

**This is the over-ship/under-ship asymmetry codeprobe-voxa was filed to
fix.** The sourcegraph keyword index returns a few precise hits per
query; the agent over-trusts that answer and stops searching. Without sg
the agent falls back to grep/Read/Bash and casts a wider net — noisy,
but it finds the files. Under "did you find what was asked?" semantics
(recall), the wider-net agent wins. Under F1 the headline reads as a
sourcegraph quality story; under recall it reads as exactly what it is —
a tool-capability artifact at the precision/recall trade-off.

The voxa fix surfaces this honestly: the headline reward (recall) tells
you who solved the task, and the IR diagnostics
(`ir_diagnostics.mean_precision`, the `overshipping` informational
warning) tell you who shipped a tighter answer. Reviewers see both
without one being smuggled into the other.

## Out of scope

* Binary scorers (test.sh exit code) and continuous scorers whose
  `metrics.json` does not emit `recall` continue to use their own score
  shape — there's no IR data to surface, and `ScoreResult.ir_metrics`
  is empty for those tasks.
* The on-disk oracle script (`mining/writer.py:_ORACLE_PY`) still
  writes `reward.txt = f1` for backward compatibility with already-mined
  task directories. The reward pivot happens in the codeprobe runner
  (`ContinuousScorer`), not in the per-task oracle, so existing tasks
  do not need to be re-mined.
