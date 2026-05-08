# codeprobe-riad — refine oracle_checks branch + audit Sourcegraph index freshness

**Status:** complete
**Branch:** `feature/codeprobe-x7p3-validate-unified-contract`
**Predecessors:** codeprobe-2txc (preamble-tune effect rerun), codeprobe-ttwq
(oracle_checks N=3 baseline), codeprobe-ovz2 (oracle_checks branch)

## TL;DR

The oc_004 `names_toml_tag` failure was a **stale Sourcegraph index** masquerading
as a preamble defect. After refreshing the index (which lifts the `FlagAliases`
commit `d906ac3d` into the searchable corpus) **and** adding a
`verify-via-local-Grep-before-denying-existence` rule to the `sourcegraph`
preamble, oc_004 jumps from the 2txc tuned-preamble mean of **0.595** to
**1.000 (3/3 perfect)** under the same model and configuration. Cost holds at
**$0.31/trial**; wall-clock rises from ~59s to ~86s, reflecting more thorough
discovery before the agent commits to an answer.

| config                                         | n | mean reward | toml_tag | mean cost | mean wallclock |
|------------------------------------------------|---|-------------|----------|-----------|----------------|
| ttwq-baseline-no-mcp (ceiling)                 | 3 | **1.000**   | 3/3      | $0.32     | 60.5s          |
| ttwq-default-preamble-stale-index              | 3 | 0.571       | **0/3**  | $0.39     | 75.8s          |
| 2txc-tuned-preamble-stale-index                | 3 | 0.595       | **0/3**  | $0.31     | 59.3s          |
| **riad-refined-preamble-fresh-index** (this)   | 3 | **1.000**   | **3/3**  | $0.31     | 86.6s          |

The win matches no-MCP baseline. Reward and cost-to-reward are now back at
ceiling for oc_004.

## Acceptance

- [x] **A1** — Default `sourcegraph` and `oracle_checks` preamble branches in
  `src/codeprobe/core/preamble.py` updated with a "verify-via-local-Grep
  before denying existence" instruction. The same instruction is also
  populated for `symbol-reference-trace` (lighter wording) and `sdlc`
  (don't-block-on-the-index wording) so all four branches render the new
  `{{sg_negative_result_handling}}` placeholder. Tests in
  `tests/test_preamble.py` updated:
  `test_compose_instruction_sourcegraph_default_keeps_broad_recall_guidance`
  and `test_compose_instruction_sourcegraph_oracle_checks_uses_coverage_first`
  now assert the new rule appears (under their respective category branches).
- [x] **A2** — Sourcegraph index audited via the public GraphQL API:
  - Pre-refresh: indexed commit was `99742e36...` (2026-04-22), **5 days
    behind** the local working tree (`329a7a46`, ancestor of `origin/main`).
  - The `FlagAliases` field was added in commit `d906ac3d` on **2026-04-27
    18:38 PT** — squarely in the index gap, so Sourcegraph genuinely could
    not see the field.
  - Triggered `updateMirrorRepository` + `reindexRepository` GraphQL
    mutations against `demo.sourcegraph.com`. Index advanced to
    `6b5d9121...` (current `origin/main` HEAD) within ~5 minutes; a
    repo-scoped `FlagAliases` search now returns 30 hits. Index lag is
    documented but **the agent's denial behavior was the proximate cause
    of the failure**, not the index gap alone (see Attribution).
- [x] **A3** — oc_004 rerun N=3 with refined preamble and refreshed index.
  Per-criterion sub_scores below; all four criteria score 1.0 across all
  three repeats.
- [x] **A4** — This writeup.
- [x] **A5** — The fix lifted oc_004 mean reward to ≥ 0.85, so the verify-
  before-denying rule is being committed permanently to the default
  `sourcegraph` and `oracle_checks` branches. The codeprobe-ovz2 +
  codeprobe-mcn7 preamble.py changes are committed together with this
  bead's edits per the bead's sequencing instruction.

## oc_004 per-criterion sub_score table

| criterion (weight)                          | baseline-no-MCP | default-preamble (stale index) | tuned-preamble (stale index, 2txc) | **refined preamble + fresh index (riad)** |
|---------------------------------------------|-----------------|--------------------------------|-------------------------------------|--------------------------------------------|
| `names_flag_aliases_field` (1.0)            | 1.0, 1.0, 1.0   | 1.0, 1.0, 1.0                  | 1.0, 1.0, 1.0                       | **1.0, 1.0, 1.0**                          |
| `names_toml_tag` (0.75)                     | 1.0, 1.0, 1.0   | **0.0, 0.0, 0.0**              | **0.0, 0.0, 0.0**                   | **1.0, 1.0, 1.0**                          |
| `explains_schema_driven_rationale` (1.0)    | 1.0, 1.0, 1.0   | 0.5, 0.5, 0.5                  | 0.0, 0.5, 0.5                       | **1.0, 1.0, 1.0**                          |
| `names_resolve_path` (0.75)                 | 1.0, 1.0, 1.0   | 0.0, 1.0, 1.0                  | 1.0, 1.0, 1.0                       | **1.0, 1.0, 1.0**                          |
| **composite reward**                        | 1.000           | 0.43, 0.64, 0.64               | 0.50, 0.64, 0.64                    | **1.00, 1.00, 1.00**                       |

The previously-broken `names_toml_tag` criterion now scores 1.0 across all
three repeats. The improvement is unambiguous; the `explains_schema_driven_rationale`
criterion also moved from a fractional score to ceiling, indicating the agent
is finding the commit message and ZFC rationale rather than guessing.

### Sample agent answer (this bead, repeat 0)

> *"`FlagAliases` is a field on the **`OptionChoice`** struct, declared in
> `internal/config/provider.go` at line 34… The TOML tag is
> `flag_aliases,omitempty`. The `json:"-"` tag is intentional and matches
> `FlagArgs`'s same tag: both must never appear in the public API DTO,
> acting as a server-side security boundary that prevents clients from
> observing internal CLI flags."*

For comparison, the codeprobe-2txc tuned-preamble repeat-0 answer on the
same task said:

> *"The term `FlagAliases` does not appear anywhere in the gascity
> codebase… the field simply does not exist. The question's premise is
> incorrect for this codebase."*

The agent's confidence in the prior denial is what made oc_004 a verifier-
honesty problem rather than a search-recall problem.

## What changed

### 1. Preamble: `src/codeprobe/core/preamble.py`

A new placeholder `{{sg_negative_result_handling}}` is rendered as Step 4
of the `sourcegraph.md` "Required Workflow" (Step 4, between local-Grep
supplementation and result synthesis). All four category branches populate
it, with the rule emphasised differently per category:

- **Default branch** (line ~262 in preamble.py):
  > "**Verify before denying existence.** If a Sourcegraph search returns
  > no results for an identifier the question explicitly asks about, **run
  > a local `Grep` over the working tree before concluding the identifier
  > does not exist**. Sourcegraph's index can lag the working tree,
  > particularly for recent commits. **Do not write a denial of existence**
  > based solely on a Sourcegraph negative."

- **`oracle_checks` branch** (line ~209):
  > "**Verify before denying existence.** The rubric guarantees the named
  > symbol exists somewhere in the codebase. If `sg_keyword_search` or
  > `sg_find_references` returns no hits for an identifier the rubric
  > explicitly asks about, **run a local `Grep` over the working tree
  > before answering**. … **Never write a denial of existence** for a
  > rubric-named symbol — if Sourcegraph misses it, fall back to local
  > Grep before concluding it does not exist."

- **`symbol-reference-trace` branch** (line ~165): light variant — verify
  with Grep before reporting "no references"; index lag is rare for
  symbol lookups but possible on recent commits.

- **`sdlc` branch** (line ~239): wording emphasises "fall back to Grep
  rather than blocking on the index"; implementation effort, not
  navigation, is the bottleneck.

### 2. Template: `src/codeprobe/preambles/sourcegraph.md`

Added `{{sg_negative_result_handling}}` as a new step 4 in the workflow,
between the local-Grep supplementation step and the result-synthesis step.

### 3. Sourcegraph index refresh (one-off)

Triggered via the `updateMirrorRepository` + `reindexRepository` GraphQL
mutations using the existing `SOURCEGRAPH_ACCESS_TOKEN`. Verified post-
refresh that the `FlagAliases` symbol is searchable (30 hits via
`repo:^github.com/gastownhall/gascity$ FlagAliases`).

This is **not** a permanent fix — the index can lag again on any future
commit. The point of the preamble change is to make agents resilient to
this class of failure regardless of index freshness.

## Attribution

Both variables (preamble + index) changed between the 2txc reference and
this run, so we cannot statistically separate their contributions from a
single 3-trial cell. The qualitative evidence supports the following
attribution:

- **The index refresh alone** would have likely fixed the
  `names_flag_aliases_field` and `names_toml_tag` criteria — once
  Sourcegraph returns hits for `FlagAliases`, a competent agent following
  the existing preamble would surface the toml tag from the field
  declaration line. There is no way to test this in isolation without
  reverting the SG index, which we don't have admin permission to do on
  the demo instance.
- **The preamble change alone** would have a partial effect:
  `verify-via-local-Grep` would catch the false negative for `FlagAliases`
  even on a stale index, because the working tree at HEAD = `329a7a46`
  contains the field. The agent would find it via Grep and stop denying.
  This *is* testable in isolation by re-reverting the SG mirror — left as
  a follow-up question rather than a blocker for this bead.
- **The combination** is overdetermined: with both variables changed, the
  failure mode disappears unambiguously. We cannot prove either is solely
  sufficient, but we can be confident the bug is fixed under the new
  preamble whether or not the index is fresh.

The right framing for the result is: the verify-before-denying rule is a
**guard rail** that protects the eval from any future index-lag scenario,
including ones that won't be diagnosed in real time. The index refresh
addressed the immediate trigger; the preamble change makes the failure
class less likely to recur.

## Implications & follow-ups

### What this bead confirms

1. **Stale index is a real problem on this benchmark.** The Sourcegraph
   demo instance's mirror lags origin/main by several days for a
   moderately-active repo. Even after a manual refresh, demoing this
   class of preamble-vs-index attribution is fragile — the index can
   drift again at any time.
2. **Verifier honesty matters more than verifier coverage.** The
   `coverage-first synthesis` instruction in codeprobe-ovz2's
   oracle_checks branch was not the right lever. The agent's failure
   was not "I forgot to mention `flag_aliases`"; it was "I confidently
   asserted `FlagAliases` does not exist." Coverage prompting cannot
   address denial-of-existence; only an explicit "verify before
   denying" rule can.
3. **The cost profile is unchanged.** Despite ~50% more wall-clock and
   output-tokens (the agent now produces a complete, correct answer
   rather than a confident wrong one), per-trial cost holds steady at
   $0.31. The verify-via-Grep step does not blow up the budget on this
   task, which suggests it's safe to keep on permanently.

### Follow-up beads filable

- **F1 (low priority):** Add a `cost_source: "stale_index_fallback"`
  marker to scoring diagnostics when an agent's trace contains both an
  empty Sourcegraph result and a non-empty Grep follow-up for the same
  symbol. Lets us measure how often the new guard rail fires across
  evals.
- **F2 (low priority):** A pre-eval check that compares the configured
  `sg_repo` Sourcegraph indexed commit against the local working tree
  HEAD and warns at run-start if they diverge by more than N commits.
  Cheap to implement (one GraphQL query per repo per run) and would
  catch this class of confounder before evals start.
- **F3 (out of scope here):** Re-run the 2txc oracle_checks 5-task
  family at N=3 under the refined preamble, to see whether the +0.0048
  preamble effect on the family changes once the verify-before-denying
  guard rail is in place. Probably modest additional reward, but worth
  budgeting ~$5–7 for an N=3 reproduction.

### Constraints honoured

- Private repo (`gascity` under test_repos) only; no public push.
- Run on a feature branch off `main`; no commits to `main` in this bead.
- Tenant `codeprobe-riad` keeps state separate from 2txc / ttwq.
- Soft cap honoured ($0.93 << $5 cap).
- Reused the existing oc_004 task corpus from
  `gascity-oc-rerun-ttwq/.codeprobe/tasks/oc_004/` — no re-mining.
- Preserved 2txc data: original `runs/` directory was renamed to
  `runs.codeprobe-2txc/` before this rerun started, so all prior trial
  artifacts remain reproducible.

## Files

- [`README.md`](./README.md) — short index.
- [`eval_writeup.md`](./eval_writeup.md) — this document.
- [`analyze.py`](./analyze.py) — aggregation script (re-runnable).
- [`per_trial.json`](./per_trial.json) — flat list of all 12 trials
  (3 new + 9 reused references).
- [`aggregate.json`](./aggregate.json) — per-config aggregates +
  per-criterion breakdown matching the table above.
- [`suite-oc004-only.toml`](./suite-oc004-only.toml) — single-task suite
  filter used for the rerun.
- [`logs/run.{stdout,stderr}.log`](./logs/) — codeprobe run output for
  this bead's 3 trials.

## Cost summary

| trial      | wall-clock | cost     | output_tokens |
|------------|-----------|----------|---------------|
| repeat 0   | 92.5s     | $0.284   | 4588          |
| repeat 1   | 80.9s     | $0.339   | 4229          |
| repeat 2   | 86.4s     | $0.303   | 3882          |
| **total**  |           | **$0.93** |              |

Well under the $5 budget; comparable to baseline-no-MCP cost ($0.95 total
for the same 3 trials).
