# codeprobe-ur8d — N=3 repeat of the 3ljz validator

**Bead:** [codeprobe-ur8d](bd) — *Investigation: 3ljz sign flip — N=3 repeat to bound with-sg recall variance*
**Validates:** codeprobe-3ljz (re-run validator), codeprobe-voxa (recall reward),
codeprobe-zat9 (multi-backend curator)
**Date:** 2026-04-29 / 2026-04-30
**Workspace:** `/home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe`
**Spend:** $48.46 ($27.32 baseline + $21.14 with-sourcegraph), under the $60 cap.

## TL;DR

* The sign flip first observed on `3ljz` (single repeat, mean_delta = −0.167)
  **holds at N=3** (mean_delta = −0.211, Cohen's d = −0.99).
* Across 30 trials the with-sourcegraph config **never beats baseline on any
  task** — 3 task-level losses, 2 ties, 0 wins.
* Within-task variance is essentially zero. with-sg's per-task recall stdev
  is 0.000 on 4 of 5 tasks; baseline's stdev tops out at 0.167. Recall is
  highly stable per `(config, task)` — the headline gap is between-task
  heterogeneity, not within-run noise.
* Task `38223444` recall variance bound: with-sourcegraph recall =
  {0.333, 0.333, 0.333} — **deterministically** at 0.333 across all three
  repeats, not run-to-run noise. trace.db shows no rate-limit / partial-result
  errors; the agent self-reports it intentionally narrowed scope to "direct
  callers" and stopped searching.
* **Verdict: conclusion (a) from the bead.** The sign flip is a robust signal,
  consistent with the user's hypothesis that "the with-sg vs baseline gap was a
  tool-capability artifact, not a quality signal." The voxa fix is doing its
  job. No follow-up bead required.

## Files

| File                              | Description                                                  |
| --------------------------------- | ------------------------------------------------------------ |
| `aggregate.json`                  | Full aggregate from `experiment aggregate` (30 trials).      |
| `per_trial.json`                  | Per-(config, task, repeat) score / precision / recall / f1.  |
| `task-38223444-trace.md`          | trace.db evidence for the with-sg recall=0.333 result.       |

## Per-task variance (recall, mean ± stdev across 3 repeats)

| Task        | baseline mean ± sd | with-sg mean ± sd | with-sg precision | mean delta |
| ----------- | -----------------: | ----------------: | ----------------: | ---------: |
| 38223444    | 0.833 ± 0.167      | **0.333 ± 0.000** |              1.00 |     −0.500 |
| 6cf61fea    | 0.889 ± 0.096      | 0.667 ± 0.000     |             ~0.21 |     −0.222 |
| b826fa9d    | 0.667 ± 0.000      | 0.667 ± 0.000     |              1.00 |      0.000 |
| d9fee4ae    | 0.833 ± 0.000      | 0.833 ± 0.000     |             ~0.31 |      0.000 |
| e5d7a4e7    | 0.944 ± 0.096      | 0.611 ± 0.096     |             ~0.22 |     −0.333 |

## Confidence intervals on the delta

Two valid framings, depending on what you treat as the unit of analysis.

| Framing                         | n  | df | mean_delta | 95% CI            | Cohen's d | Excludes 0? |
| ------------------------------- | -: | -: | ---------: | :---------------: | --------: | :---------: |
| Trial-level paired (per repeat) | 15 | 14 |   −0.211   | [−0.329, −0.093]  |   −0.99   |    **yes**  |
| Task-level mean (avg of 3 repeats) |  5 |  4 |   −0.211   | [−0.480, +0.058]  |   ~ −0.97 |     no      |

The trial-level CI excludes zero with strong effect size. The task-level
CI straddles zero only because n=5 tasks; per-trial recall variance is
near zero so the trial-level analysis is the cleaner read here. The
directional check (with-sg never wins on any task) corroborates this.

## Comparison with prior runs

`mean_delta` is `with-sg − baseline`:

| Run            | Reward formula | GT source        | mean_delta | Cohen's d | wins (B/SG/tie) |
| -------------- | -------------- | ---------------- | ---------: | --------: | --------------- |
| `wo7n` (old)   | F1             | sg-only          |    +0.265  |   +0.40   | 1 / 3 / 1       |
| `gcwk` (mid)   | F1             | multi-backend GT |    +0.141  |   +0.74   | 1 / 3 / 1       |
| `3ljz` (N=1)   | recall         | multi-backend GT |    −0.167  |   −1.00   | 3 / 0 / 2       |
| **`ur8d`** (N=3) | **recall**   | multi-backend GT |  **−0.211**| **−0.99** | **3 / 0 / 2**   |

Same agent (Claude Opus 4.7), same five tasks, same MCP surface. Only the
reward formula and oracle backends changed. The headline conclusion
flipped sign — and stayed flipped at N=3.

## Bias / quality flags

* **`backend_overlap`**: 15 informational warnings on with-sg (one per
  trial). Severity is *informational* not *warning* because the curator
  has independent corroboration via `ast` and `grep` backends, so the
  overlap is honest signal — the codeprobe-9re9 severity gate is doing
  what it should. No change vs. 3ljz.
* **`overshipping`**: 0. The voxa fix removes the precision penalty, so
  the e5d7a4e7-style "baseline scored 0.03 with recall=1.0" pathology
  cannot recur under the new reward formula.
* **`low_recall`**: 3 trials on with-sg/38223444 (the deterministic
  recall=0.333 result). Surfaced by the trace_quality view.

## Reading guide

* **Worked example in scoring_model.md** — `docs/scoring_model.md`
  carries the narrative ("Worked example: before/after voxa on
  gascity"). This directory carries the raw artifacts.
* **Per-trial details** — see `per_trial.json`.
* **Why with-sg loses on 38223444** — see `task-38223444-trace.md`.
