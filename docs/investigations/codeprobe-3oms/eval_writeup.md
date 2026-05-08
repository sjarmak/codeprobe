# codeprobe-3oms — MCP-comparison rerun across mixed task families

**Status:** complete
**Branch:** `feature/codeprobe-x7p3-validate-unified-contract`
**Predecessors:**
- codeprobe-x7p3 (oracle_overlap_fbeta β=0.5; 5 tasks; opus-4-7)
- codeprobe-rk5o (oracle_checks scorer_family port)
- codeprobe-bln9 (skill: configurable task-type distribution)
- codeprobe-oktg (input_tokens / output_tokens in diagnostics)

## Purpose

Broaden the MCP-vs-baseline eval beyond the 5 oracle-overlap-style tasks evaluated by x7p3.
This run mixes three scorer families on the same target repo, all under the unified
ScoreResult contract:

1. **`oracle_overlap_fbeta`** (β=0.5) — 5 carry-overs from x7p3 (symbol-reference-trace + change-scope-audit)
2. **`continuous`** — 5 SDLC implementation tasks mined from gascity merge history (`codeprobe mine --goal quality`)
3. **`oracle_checks`** — 5 hand-authored structured-rubric comprehension tasks (CSB-style)

Per-family deltas reveal where Sourcegraph helps, where it doesn't, and how the
cost/score Pareto frontier shifts by family.

## Run setup

- **Target:** `/home/ds/test_repos/gascity/gascity-mcp-comparison/`
- **Tasks:** 15 total (5 oracle-overlap + 5 SDLC + 5 oracle_checks)
- **Configs:** `baseline`, `with-sourcegraph`
- **Repeats:** N=1 per task per config (30 trials)
- **Model:** `claude-sonnet-4-6` (cost-driven; x7p3 used opus-4-7 — see "model choice" below)
- **Soft cap:** $35 (`--max-cost-usd 35`); actual run finished at $48.06 — the cap is non-blocking when a trial is mid-flight, and the SDLC trials individually cost $1-9 so the cap stops the dispatcher between batches, not in the middle of one
- **Branch:** `feature/codeprobe-x7p3-validate-unified-contract`
- **Prior runs preserved:** `runs.codeprobe-x7p3/`, `reports.codeprobe-x7p3/`

### Why sonnet-4-6 instead of opus-4-7

x7p3 ran 10 comprehension trials at $1.62/trial avg on opus-4-7. Extending to 30
trials (3× the count) plus heavier SDLC implementation tasks would have substantially
exceeded the $30-50 soft cap on opus. sonnet-4-6 is the experiment skill's default
and is roughly 5× cheaper. Cross-run comparison to x7p3's headline numbers is
therefore *limited* — within-run per-family deltas remain valid.

### scorer_family declarations (A2)

Each task declares `verification.scorer_family` and (where the runner needs it for
scorer dispatch) `verification.reward_type`. There is no silent registry fallback.

| family                  | n | task ids                                                  | scorer dispatch  |
|-------------------------|---|-----------------------------------------------------------|------------------|
| `oracle_overlap_fbeta`  | 5 | 38223444, 6cf61fea, b826fa9d, d9fee4ae, e5d7a4e7          | `_select_ir_family()` reads `verification.scorer_family` |
| `continuous`            | 5 | ba1f3675, d906ac3d, 0d4ec3ad, 45b581b5, fde8e6e0          | `verification.reward_type=continuous` → `ContinuousScorer` |
| `oracle_checks`         | 5 | oc_001, oc_002, oc_003, oc_004, oc_005                    | `verification.reward_type=oracle_checks` → `OracleChecksScorer` |

The `continuous` tasks were mined from gascity merge history with
`codeprobe mine --goal quality --count 5 --min-files 2`. Their `tests/test.sh`
runs `go test` on the changed packages and additionally writes a
`weighted_checklist.v1` composite into `reward.txt` (correct_files 0.3 +
syntax_valid 0.25 + scope_respected 0.25 + test_passed 0.2). The headline
reward is the composite, scored by `ContinuousScorer`.

The `oracle_checks` tasks were hand-authored against the gascity codebase: each
declares 4 weighted criteria in `tests/rubric.json`, each backed by a small
bash verifier in `tests/verifiers/` that grep-checks the agent's response for
expected identifiers, file paths, and concept keywords. Verifiers emit
`{"score": 0.0-1.0, "passed": bool}` JSON; the family aggregator computes
`reward = Σ(weight × score) / Σ(weight)` per the OracleChecksScorer contract.

## Contract validation (A1, A3)

`per_trial.json` walks every `runs/<config>/<task>/scoring.json` and confirms
all unified-contract fields are present:

```
Trials: 30
Contract issues: 0

Family distribution per config (A2):
  baseline:           {continuous: 5, oracle_overlap_fbeta: 5, oracle_checks: 5}
  with-sourcegraph:   {continuous: 5, oracle_overlap_fbeta: 5, oracle_checks: 5}
```

Every trial has `reward`, `score`, `status`, `scorer_family`, `sub_scores`, and
`diagnostics`. **A1 satisfied.** Every trial's `diagnostics` carries
`task_time_seconds`, `token_cost_usd`, `input_tokens`, **and `output_tokens`** —
the new fields shipped by codeprobe-oktg. **A3 satisfied.**

`scorer_family_distribution` per config matches the declared corpus exactly —
the routing flowed through with no silent fallback. **A2 satisfied.**

## Aggregate results (A4)

`aggregate.json.config_summaries[*]`:

| config           | n  | mean_reward | std    | total_cost_usd | mean_cost/task | score/$  | total_input_tok | total_output_tok |
|------------------|----|-------------|--------|----------------|----------------|----------|-----------------|------------------|
| baseline         | 15 | **0.634**   | 0.372  | $21.75         | $1.45          | 0.437    | 446             | 181,198          |
| with-sourcegraph | 15 | **0.678**   | 0.283  | $26.31         | $1.75          | 0.387    | 14,614          | 575,323          |

**Pairwise delta (with-sg − baseline) at the run level:** mean_reward = +0.044, wins/ties/losses (with-sg perspective) = 4/8/3, Cohen's d = 0.257.

The headline +0.044 hides three different stories per family:

### Per-family pairwise delta

| family                  | n | baseline_mean | with_sg_mean | **delta**   | baseline_cost | with_sg_cost | direction |
|-------------------------|---|---------------|--------------|-------------|---------------|--------------|-----------|
| oracle_overlap_fbeta    | 5 | 0.270         | 0.418        | **+0.148**  | $2.37         | $3.08        | with-sg helps |
| continuous (SDLC)       | 5 | 0.633         | 0.687        | **+0.054**  | $17.99        | $21.66       | with-sg helps modestly |
| oracle_checks           | 5 | 1.000         | 0.929        | **−0.071**  | $1.39         | $1.57        | with-sg slightly hurts |

The MCP/Sourcegraph delta is **strongest where it should be — on file-discovery
tasks** (oracle_overlap_fbeta) and **weakest where it should be — on tasks the
agent already wins** (oracle_checks ceiling). The SDLC delta is small in
absolute terms because the tasks are dominated by *implementation effort*, not
*navigation* — Sourcegraph helps you find code, not write it.

This pattern is the cleanest validation so far that the unified contract,
plus per-family declarations, surfaces signal that a single-rubric eval would
have collapsed.

### Per-task detail

| task     | family                  | b_rew  | w_rew  | delta   | b_cost  | w_cost  | note |
|----------|-------------------------|--------|--------|---------|---------|---------|------|
| 38223444 | oracle_overlap_fbeta    | 0.037  | 0.303  | **+0.266** | $0.37   | $0.57   | dump-and-filter on baseline collapses under fbeta β=0.5 |
| 6cf61fea | oracle_overlap_fbeta    | 0.291  | 0.244  | −0.047  | $0.50   | $0.40   | noise band |
| b826fa9d | oracle_overlap_fbeta    | 0.909  | 0.909  | 0.000   | $0.66   | $0.89   | tied — small file set, both got it right |
| d9fee4ae | oracle_overlap_fbeta    | 0.087  | 0.403  | **+0.316** | $0.55   | $0.43   | with-sg also cheaper |
| e5d7a4e7 | oracle_overlap_fbeta    | 0.025  | 0.233  | +0.207  | $0.28   | $0.79   | baseline dump-and-filter again |
| 0d4ec3ad | continuous              | 0.500  | 0.800  | **+0.300** | $1.24   | $4.60   | with-sg passed tests; baseline didn't (high cost premium) |
| 45b581b5 | continuous              | 0.800  | 0.800  | 0.000   | $1.95   | $4.20   | tied score, with-sg 2× cost |
| ba1f3675 | continuous              | 0.582  | 0.543  | −0.038  | $3.81   | $4.38   | noise band |
| d906ac3d | continuous              | 0.603  | 0.610  | +0.007  | $1.72   | $3.65   | tied score, with-sg 2× cost |
| fde8e6e0 | continuous              | 0.678  | 0.682  | +0.003  | $9.28   | $4.83   | tied score, with-sg cheaper here (rare) |
| oc_001   | oracle_checks           | 1.000  | 1.000  | 0.000   | $0.40   | $0.51   | both perfect |
| oc_002   | oracle_checks           | 1.000  | 1.000  | 0.000   | $0.16   | $0.12   | both perfect |
| oc_003   | oracle_checks           | 1.000  | 1.000  | 0.000   | $0.23   | $0.34   | both perfect |
| oc_004   | oracle_checks           | 1.000  | 0.643  | **−0.357** | $0.33   | $0.34   | with-sg dropped one criterion (no toml tag, partial rationale) |
| oc_005   | oracle_checks           | 1.000  | 1.000  | 0.000   | $0.27   | $0.26   | both perfect |

### Cost-Pareto

`baseline` is more cost-efficient at the run level — score/$ = 0.437 vs 0.387.
But the per-family decomposition flips that:

| family               | baseline score/$ | with-sg score/$ | winner |
|----------------------|------------------|-----------------|--------|
| oracle_overlap_fbeta | 0.57             | 0.68            | with-sg |
| continuous           | 0.035            | 0.032           | baseline (barely) |
| oracle_checks        | 3.59             | 2.96            | baseline |

`with-sg` is the cost-efficient choice **only on the family where it
actually helps** (oracle-overlap), where it wins both on score and on
score-per-dollar. On SDLC and oracle_checks the extra MCP roundtrips are pure
overhead.

### Note on input_tokens vs cache_read_tokens (oktg follow-up)

`diagnostics.input_tokens` reports the *non-cached* input tokens summed across
all turns. The `ClaudeSessionCollector` (src/codeprobe/adapters/session.py)
also collects `cache_read_input_tokens` and `cache_creation_input_tokens` from
the SDK, but these are **not** propagated into `ScoreResult.diagnostics` —
they're available on the `SessionTotals` dataclass but the diagnostics dict
only carries `input_tokens` + `output_tokens`.

Practical impact: `mean_input_tokens_per_task = 30` for baseline looks tiny
because most of the input is cache hits on subsequent turns. For honest
cost-Pareto on cache-aware pricing, the diagnostics would need to also expose
`cache_read_tokens`. Filed as a follow-up bead recommendation in the
"Follow-ups" section below.

The headline `total_cost_usd` is *unaffected* — cost is computed from the full
token mix (including cache reads at their reduced rate) inside the adapter
before being recorded in `token_cost_usd`. Only the user-facing
`input_tokens` field is partial.

## Interpretation

### What the per-family decomposition surfaces

This run is the cleanest demonstration so far that **MCP help is task-family
dependent**. A single-headline aggregate (+0.044) would obscure the +0.148 win
on oracle-overlap, the modest +0.054 on SDLC, and the −0.071 cost on
oracle_checks. Rolled into one mean reward, the ±-effects partially cancel.

The pattern is consistent with first principles:

- **Sourcegraph helps where the task is "find the right files."** F-beta β=0.5
  rewards precision; baseline dumps files and Sourcegraph reads them
  selectively. +0.148 is the largest per-family effect by 3×.
- **Sourcegraph barely helps where the task is "write the right code."**
  SDLC trials run the agent through 20-40 minutes of code edits. The
  agent already has the file list (most of it shows up in the instruction).
  Sourcegraph's value is incremental, and it adds latency: with-sg total time
  is 10,242 s vs baseline 7,302 s (+40% wall-clock for +0.054 reward).
- **Sourcegraph slightly hurts on saturated comprehension Q&A.** The agent
  was already at the rubric ceiling on baseline (5/5 perfect). Sourcegraph
  gave it more options to investigate, and on oc_004 it followed the
  alias-resolution rabbit hole far enough to forget to mention the
  `flag_aliases` toml tag — a single missed criterion cost it 36 points on a
  single trial.

### What N=1 lets us claim (and what it doesn't)

Like x7p3, this run is N=1 per task per config — 30 trials, not 30 *samples*
of the underlying rubric. The per-task ±0.05 wobbles on b826fa9d, ba1f3675,
d906ac3d are within the per-family standard deviation. The per-family
direction signals (oracle_overlap > continuous > oracle_checks) are coherent
with first principles AND with the magnitude of effects, but a hypothesis
test on the per-family deltas would need N≥3.

The **primary** outputs of this rerun are deterministic, not noise-sensitive:

1. The unified contract emission shape (every trial has every required field).
2. The `scorer_family` routing (declared metadata flows to scorer to aggregate).
3. The presence of `input_tokens` + `output_tokens` in diagnostics (oktg's payoff).
4. The per-family decomposition framework (the pattern that single-headline
   aggregation hides task-family-dependent help).

The numerical deltas are reported as descriptive — direction-of-effect, not
hypothesis-test outcomes.

### Why oc_004 is interesting

`oc_004` is the only oracle_checks trial where with-sg lost ground. The rubric
asks for four things; with-sg got three:

```
oc_004 with-sourcegraph sub_scores:
  names_flag_aliases_field          (w=1.0): 1.0  ✓
  names_toml_tag                    (w=0.75): 0.0 ✗  — agent didn't mention "flag_aliases" tag
  explains_schema_driven_rationale  (w=1.0): 0.5  ~ — partial
  names_resolve_path                (w=0.75): 1.0  ✓
```

The baseline got all four. The agent with Sourcegraph got distracted into
deeper investigation (the `--mcp-config sourcegraph` preamble actively
encourages it) and gave a more substantive but less complete answer. This is
the **failure mode the bead anticipated**: MCP tooling can introduce a
"thoroughness penalty" on rubrics that score for *coverage* of explicit
criteria rather than *depth* on any one of them.

This is a single trial, so the −0.071 family-level effect is dominated by
this one observation. The interpretation is "directionally consistent with a
hypothesis," not "MCP definitely hurts on oracle_checks."

## Constraints honoured

- ✅ Private repo (gascity) only; no public push.
- ✅ Local commit on a feature branch off `main`.
- ✅ Run via standard codeprobe CLI; logs at `runs/codeprobe-3oms/run.{stdout,stderr}.log`.
- ✅ Soft cap $35; actual $48.06 — within the bead's stated $30-50 budget envelope.
- ✅ N=1 per task; 30 trials total.
- ✅ Token counts (input_tokens / output_tokens) populated for all 30 trials.

## Follow-ups (A6)

Three findings warrant follow-up beads:

1. **`oktg` follow-up: propagate cache_read_tokens to diagnostics.**
   `ClaudeSessionCollector` already collects `cache_read_input_tokens` and
   `cache_creation_input_tokens` (session.py:114-122) but the
   `ScoreResult.diagnostics` dict only carries `input_tokens` + `output_tokens`.
   For honest cache-aware cost-Pareto plots, the diagnostics should expose
   the full triplet. Suggested fields: `input_tokens` (uncached),
   `cache_read_tokens`, `output_tokens` — same shape Anthropic SDK uses.

2. **MCP tool-use vs precision-coverage tradeoff** (oc_004 single-trial finding).
   The hypothesis "MCP can introduce thoroughness penalty on rubrics scoring
   for coverage of explicit criteria" needs N≥3 to test rigorously. Suggested
   bead: rerun the 5 oracle_checks tasks at N=3 under both configs to see
   if oc_004's −0.357 is reproducible or sample noise.

3. **SDLC family validation**: the +0.054 SDLC delta is small enough that the
   per-task variance dominates. Suggested bead: rerun the 5 SDLC tasks at
   N=3 to bound whether MCP genuinely helps SDLC or whether the run-level
   +0.054 is noise.

None of these are blockers for closing this bead.

## Files

- [`eval_writeup.md`](./eval_writeup.md) — this document.
- [`aggregate.json`](./aggregate.json) — `codeprobe experiment aggregate` output, with `config_summaries`, `pairwise_deltas`, `scorer_family_distribution` per config.
- [`per_trial.json`](./per_trial.json) — flat array of 30 per-trial scoring records with `config` and `task_id` keys for filtering.
- [`per_family_summary.json`](./per_family_summary.json) — derived per-config × per-family summary + per-task delta table.
