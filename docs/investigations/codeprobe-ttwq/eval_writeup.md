# codeprobe-ttwq — oracle_checks N=3 rerun (MCP-thoroughness penalty)

**Status:** complete
**Branch:** `feature/codeprobe-x7p3-validate-unified-contract`
**Predecessor:** codeprobe-3oms — see [eval_writeup.md](../codeprobe-3oms/eval_writeup.md)

## Purpose

codeprobe-3oms found that `with-sourcegraph` slightly hurt `oracle_checks` performance (per-family delta = −0.071 at N=1):

- baseline: 5/5 perfect (mean 1.000)
- with-sourcegraph: 4/5 perfect (mean 0.929) — `oc_004` dropped to 0.643

The 3oms charitable hypothesis was a "thoroughness penalty": Sourcegraph encouraged deeper investigation that distracted the agent from the rubric's coverage criteria. The agent reportedly forgot to mention the `flag_aliases` toml tag (one of four rubric criteria, weight 0.75).

But it was a single trial. The −0.071 family-level effect was dominated by `oc_004`'s −0.357. To turn this into a real finding, we ran N=3.

## Run setup

- **Target:** `/home/ds/test_repos/gascity/gascity-oc-rerun-ttwq/` (sister of `gascity-mcp-comparison/`; isolated to avoid checkpoint.db contention with the SDLC rerun in codeprobe-mcn7).
- **Tasks:** 5 oracle_checks (`oc_001`..`oc_005`).
- **Configs:** `baseline`, `with-sourcegraph` (Sonnet 4.6 on both; with-sourcegraph adds the Sourcegraph MCP HTTP server).
- **Repeats:** N=3 per task per config (30 trials).
- **Soft cap:** $15 (`--max-cost-usd 15`); actual run cost $9.85.
- **Tenant:** `codeprobe-ttwq` (custom, to avoid the gascity tenant lock held by codeprobe-mcn7).
- **Scorer:** `oracle_checks` (composite of weighted bash-verifier criteria; reward = Σ(weight × criterion_score) / Σ(weight)).

## Aggregate results

`aggregate.json` (codeprobe interpret output) summary:

| config           | n  | mean_reward | std    | median | total_cost | mean_cost | total_input_tok | total_output_tok | total_cache_read_tok |
|------------------|----|-------------|--------|--------|------------|-----------|-----------------|------------------|----------------------|
| baseline         | 15 | **1.000**   | 0.000  | 1.000  | $4.63      | $0.31     | 111             | 27,262           | 2,951,942            |
| with-sourcegraph | 15 | **0.914**   | 0.184  | 1.000  | $5.23      | $0.35     | 634             | 76,725           | 5,047,122            |

- 15/15 baseline trials scored 1.0 — saturated rubric ceiling.
- 12/15 with-sg trials scored 1.0; the 3 failures are all on `oc_004`.

### Family-level delta (paired-t over 15 within-task pairs)

- delta = **−0.0857**
- t = −1.81, df = 14
- 95% CI = [−0.187, **+0.016**]
- p ≈ 0.092 (two-sided)

**Family-level result: not statistically significant at α=0.05** (CI just barely crosses zero). The codeprobe interpret output flagged the same: "baseline nominally ahead (not significant at p=0.05)."

### Per-task detail

| task   | baseline (3 reps) | with-sg (3 reps)    | mean delta | paired-t  | 95% CI            | passed? |
|--------|-------------------|---------------------|------------|-----------|-------------------|---------|
| oc_001 | [1.0, 1.0, 1.0]   | [1.0, 1.0, 1.0]     | 0.000      | n/a       | [0, 0]            | yes |
| oc_002 | [1.0, 1.0, 1.0]   | [1.0, 1.0, 1.0]     | 0.000      | n/a       | [0, 0]            | yes |
| oc_003 | [1.0, 1.0, 1.0]   | [1.0, 1.0, 1.0]     | 0.000      | n/a       | [0, 0]            | yes |
| oc_004 | [1.0, 1.0, 1.0]   | [0.643, 0.429, 0.643] | **−0.429** | **−6.000**  | [−0.736, −0.121]  | **no** |
| oc_005 | [1.0, 1.0, 1.0]   | [1.0, 1.0, 1.0]     | 0.000      | n/a       | [0, 0]            | yes |

**oc_004: 3/3 with-sg trials below ceiling, all baseline trials at ceiling. Mean delta −0.429 with t=−6.0 is statistically reliable per-task even at N=3.**

## A4 — is the −0.357 from 3oms reproducible?

**Yes, the direction and magnitude are reproducible — and arguably worse than 3oms suggested.** All three with-sourcegraph oc_004 trials underperformed the baseline ceiling. Per-trial scores: 0.643, 0.429, 0.643 (mean 0.571, std 0.124). Compared to 3oms's single 0.643, the N=3 distribution is centered slightly *below* the 3oms point estimate.

## Why oc_004 fails under Sourcegraph — diagnosis

I read all three with-sourcegraph oc_004 `agent_output.txt` files. The failure mode is **not** the "thoroughness penalty" hypothesized in 3oms. Every with-sg trial confidently declared:

> **`FlagAliases` does not exist in the gascity codebase.**

Sample wording from the three trials:

> *"After exhaustive searching across all files, commits, and diffs in the `github.com/gastownhall/gascity` repository, **`FlagAliases` does not exist** in this codebase."*

> *"Based on a thorough search of the indexed repository — current code, diffs, and all commit history — **`FlagAliases` does not exist in the gascity codebase**."*

> *"After a thorough search of the repository — including all files under `internal/config/`, the full git commit history, and all diff records — I must be direct: **`FlagAliases` does not exist in the gascity codebase**."*

This is wrong. `FlagAliases` is declared at `internal/config/provider.go:34` with toml tag `flag_aliases,omitempty`, used by `CollectAllSchemaFlags` in `options.go`, cloned into `pack.go`, and threaded through `resolve.go`/`resolved_cache.go`. The baseline trials all (3/3) found and cited it correctly using local Grep/Read.

### Why partial credit instead of zero

The composite still scored 0.43–0.64 (not 0.0) because the rubric verifiers are literal-string matchers:

- `names_flag_aliases_field` (weight 1.0): grep for `FlagAliases` AND `provider.go|internal/config` in the agent's output. The agent's *denial* contains both strings → 1.0.
- `names_toml_tag` (weight 0.75): grep for `flag_aliases`. The agent never wrote that tag → 0.0.
- `explains_schema_driven_rationale` (weight 1.0): partial credit (0.5–1.0) for the agent's discussion of `FlagArgs`/OptionsSchema (the closest thing it found and wrote about).
- `names_resolve_path` (weight 0.75): hits when the trial mentioned `ResolveProviderBaseChain` or `stripArgsSlice`/`specToResolved` in its discussion of what it did find.

So a confidently-wrong answer that names FlagAliases (to deny it) and discusses FlagArgs as a "closest analog" passes ~55–70% of the weighted rubric. **The verifier scores literal coverage, not correctness.** This is a known property of the oracle_checks scorer family (see `docs/scoring_model.md`), and an honest-but-blunt instrument here.

### Mechanism: Sourcegraph false negative cascade

The likeliest explanation, based on the agent's wording ("indexed repository", "exhaustive searching", repeated framing of having checked "diffs and commit history"):

1. The Sourcegraph MCP `searchCode` tool was the agent's primary search modality.
2. It returned no hits for `FlagAliases` (or returned a misleading set; the indexed snapshot may lag local working tree, or the index covers a stale branch).
3. The agent treated the negative Sourcegraph result as authoritative — "I checked the indexed repo exhaustively" — and stopped looking with local Grep.
4. It then composed a confident-sounding denial that cited "indexed repo + commit history + diffs" to justify the conclusion.

Baseline runs go straight to local Grep/Read on the working tree, which trivially finds the field.

### Reframing the 3oms hypothesis

The 3oms charitable read ("thoroughness penalty — agent forgot toml tag") undersold the problem. The actual mechanism is a **trust-the-tool false-negative cascade**: when the MCP tool returns "not found," the agent treats it as authoritative even when local Grep would contradict it. This is a higher-stakes failure than missing a single rubric criterion — the agent fabricates a denial of existence.

This generalizes beyond oc_004: any task whose ground truth is in code that Sourcegraph happens not to index (private branches, recent commits, reflog-only commits, vendored code) is at risk. The MCP makes the agent *more confident* in a wrong negative.

## Cost-Pareto

| family               | n  | baseline mean | with-sg mean | delta   | baseline cost | with-sg cost | b score/$ | w score/$ | winner |
|----------------------|----|---------------|--------------|---------|---------------|--------------|-----------|-----------|--------|
| oracle_checks (this) | 15 | 1.000         | 0.914        | −0.086  | $4.63         | $5.23        | 3.24      | 2.62      | baseline |

with-sourcegraph is more expensive *and* slightly worse on this family. The cache-read tokens roughly double under with-sg (5.0M vs 2.95M) — Sourcegraph adds cached MCP turns even when it returns null results, which is part of the cost overhead.

## Reconciliation with 3oms headline

3oms reported per-family delta = −0.071 (N=1). N=3 reports −0.0857 — the magnitude is consistent within sampling. The shape changed: 3oms attributed the loss to one mid-range slip; N=3 shows it concentrated entirely on oc_004 with no signal on the other four tasks (which all sit at the ceiling for both configs).

This means **the family-level signal is task-driven, not config-driven, on this corpus**. The MCP penalty is real on oc_004 (where local-grep beats Sourcegraph because Sourcegraph's index misses the relevant file or returns no hits), and zero everywhere else (where both configs hit the rubric ceiling).

## Acceptance

- [x] **A1** — 30 trials produced (5 oracle_checks × 2 configs × N=3). See `aggregate.json` and `per_trial.json`.
- [x] **A2** — Per-task mean reward + std across 3 repeats reported (table above and `per_family_summary.json`).
- [x] **A3** — Per-family delta with paired-t test reported (95% CI [−0.187, +0.016]; t=−1.81 df=14).
- [x] **A4** — oc_004 reproducibility addressed: 3/3 with-sg trials underperformed the baseline ceiling. Per-task delta = −0.429 with paired-t = −6.0, 95% CI = [−0.736, −0.121]. The 3oms point estimate (single trial at 0.643) sits inside the N=3 distribution (mean 0.571).
- [x] **A5** — Writeup at `docs/investigations/codeprobe-ttwq/eval_writeup.md`.

## Artifacts

- `per_trial.json` — flat list of 30 trials with reward, cost, tokens, scorer_family.
- `per_family_summary.json` — per-config / per-task aggregates and paired-t deltas.
- `aggregate.json` — `codeprobe interpret --format json` envelope.
- `analyze.py` — per-task aggregation and paired-t computation.
- Run artifacts (preserved): `/home/ds/test_repos/gascity/gascity-oc-rerun-ttwq/.codeprobe/runs/{baseline,with-sourcegraph}/`. Each per-task directory contains the last-trial agent_output.txt and scoring.json; per-repeat subdirectories (`repeat-1/`, `repeat-2/`) preserve the earlier trials' agent_output and scoring.

## Follow-ups

- **Verifier honesty on oc_004**: The literal-string verifier gives 0.43–0.64 to a confidently-wrong denial. Adding an explicit "must affirm field exists" criterion (or an LLM judge that scores correctness, not coverage) would push these trials closer to 0.0 and surface the true MCP failure mode in the headline number. This belongs to the broader scorer-honesty work tracked under codeprobe-rk5o follow-ups.
- **Sourcegraph index drift**: confirm whether `gastownhall/gascity` is indexed at the same commit the local working tree is on. If the index is stale, the with-sg false-negative behavior is partly a Sourcegraph-side caching issue; if it's current, the MCP returns wrong negatives on present code.
- **Generalize the false-negative test**: synthesize a small adversarial corpus where the ground truth lives in code Sourcegraph is known to miss (e.g., uncommitted, gitignored vendor copies, recent rebases). Predict that with-sg delta becomes large-negative across the corpus, confirming the mechanism beyond a single task.
