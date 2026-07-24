# Scoring model — reward, scorer_family, and IR diagnostics

> codeprobe-voxa (revised 2026-04-30). Pairs with the multi-backend
> oracle curator (codeprobe-zat9) and the bias-detection severity gate
> (codeprobe-9re9). Replaces the original 2026-04-29 voxa pass that
> used recall-as-reward universally.

## TL;DR

Every `ScoreResult` now carries a **`scorer_family`** declaring *which
rubric produced the reward*. The default IR family is
`oracle_overlap_f1` — F1 penalises both over-shipping (low precision)
and under-shipping (low recall), so an agent that dumps every file in
the repo no longer scores 1.0. File-discovery / triage tasks where dump-
and-filter is appropriate opt into `oracle_overlap_recall` per-task via
`metadata.json#verification.scorer_family`.

| Concept            | Question it answers                                        | Where to find it                                                                            |
| ------------------ | ---------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| **Reward**         | Did the agent solve the task under its declared rubric?    | `ScoreResult.score` / `ScoreResult.reward_score` / `mean_automated_score` / `mean_reward`   |
| **scorer_family**  | Which rubric was used? Why does this number mean what it does? | `ScoreResult.scorer_family` / `scoring_details.scorer_family` / `scorer_family_distribution` |
| **sub_scores**     | What inputs produced the reward? (e.g. `{recall, precision, f1, reward}`) | `ScoreResult.sub_scores` / `scoring_details.sub_scores`                                     |
| **Diagnostics**    | Run-time observations that don't change reward             | `ScoreResult.diagnostics.ir_metrics` (mirrors `ir_metrics`) and `ScoreResult.ir_metrics`    |

## Why per-task scorer_family

The original voxa pass made `score = recall` for every IR scorer. That
fixed one failure mode (over-shipping not penalised) by introducing
another (no per-task rubric). Under recall-only, an agent that dumps
the entire repository scores 1.0 — the recall is genuinely 1.0, but the
agent didn't solve anything in any meaningful sense, and any tooling
decision based on that headline ("does MCP help? which agent is
better?") would be wrong.

The reopen ships a **per-task scorer_family registry**:

* `symbol-reference-trace` style tasks (and any IR task where the
  oracle's answer set is the answer) use **`oracle_overlap_f1`** by
  default. Reward is F1 — it goes up when the agent finds the truth,
  down when it ships noise, and down when it misses. Recall stays in
  `sub_scores` so reviewers can still see "found everything" vs "found
  nothing."
* Tasks where dumping a wide net is the *expected behavior*
  (file-discovery, triage, exploratory codebase search) opt into
  **`oracle_overlap_recall`** — over-shipping is free, only missing
  costs.
* The org-scale tier-weighted oracle uses **`oracle_weighted_f1`** by
  default (weighted F1, where the on-disk oracle stores the weighted
  primary score in the `f1` field when `metric == "weighted_f1"`).
  Tasks that prefer the recall-tilted variant pick
  **`oracle_weighted_recall`**.

The family is recorded on every `ScoreResult` and propagated into
`completed.json#scoring_details` and the per-config
`scorer_family_distribution` block in `aggregate.json`. Mixed-family
configs (e.g. half a run on `oracle_overlap_f1`, half on
`oracle_overlap_recall`) are visible in the aggregate, and reviewers can
reason about whether the comparison is apples-to-apples.

## The full registry

```python
SCORER_FAMILIES = frozenset({
    # IR-style — oracle is a set of expected files / symbols
    "oracle_overlap_f1",        # default for symbol-reference-trace, file-list-tight
    "oracle_overlap_recall",    # opt-in for file-discovery / triage
    "oracle_weighted_f1",       # default for org-scale tier-weighted oracle
    "oracle_weighted_recall",   # opt-in for tier-weighted recall-tilted

    # Sequence-style — order matters
    "sequence_lcs",             # dependency_chain (LCS / max_len)

    # Equality / scalar
    "exact_match",              # count, boolean, text

    # Test-script style — verifier emits reward directly
    "binary_test",              # test.sh exit code → 0.0/1.0
    "continuous",               # reward.txt or stdout float, no IR

    # Composite
    "weighted_checkpoints",     # CheckpointScorer
    "oracle_checks",            # OracleChecksScorer (structured rubric — CSB-aligned)
    "dual_composite",           # DualScorer (direct + artifact)
})
```

Adding a new family: extend `SCORER_FAMILIES`, document the rubric and
sub_scores keys here, and add a fixture-backed test in
`tests/test_scoring_reward.py` covering null / golden / adversarial
inputs.

## Reward formulas by family

```
oracle_overlap_f1        → reward = f1   (= 2·P·R / (P+R))
oracle_overlap_recall    → reward = recall
oracle_weighted_f1       → reward = f1 (weighted on-disk; falls back to weighted_recall, then recall)
oracle_weighted_recall   → reward = weighted_recall (falls back to recall)
sequence_lcs             → reward = LCS(expected, actual) / max(len(expected), len(actual))
exact_match              → reward = 1.0 if equal else 0.0
binary_test              → reward = 1.0 if exit==0 else 0.0
continuous               → reward = clamped reward.txt (or last stdout line)
weighted_checkpoints     → reward = Σ (weight_i · checkpoint_score_i)
oracle_checks            → reward = Σ (weight_i · criterion_score_i) / Σ weight_i
dual_composite           → reward depends on scoring_policy:
                              ""        → reward_direct
                              "min"     → min(direct, artifact)
                              "mean"    → (direct + artifact) / 2
                              "gate"    → 1.0 if both passed else 0.0
                              "weighted"→ weight_d·direct + weight_a·artifact
```

All values live in `[0.0, 1.0]`. Reward computation is mechanical — no
thresholds, no soft-clipping, no judgment. The family declares which
formula to apply; the formula itself is pure arithmetic on the inputs.

### `weighted_checkpoints` verifier stdout

Checkpoint verifiers have two mutually exclusive result channels:

* Non-empty stdout is the JSON channel. After surrounding whitespace is
  removed, it must be one JSON object. Its `score` field is converted to a
  number and clamped to `[0.0, 1.0]`; an omitted `score` defaults to `0.0`.
  Invalid JSON, a non-object JSON value, or a non-numeric `score` logs a
  warning naming the verifier and scores `0.0`, regardless of exit status.
* Completely empty stdout is the legacy exit-code channel: exit `0` scores
  `1.0`, and a nonzero exit scores `0.0`.

Verifier diagnostics therefore belong on stderr. A warning or other
non-JSON text on stdout does not fall back to exit-code success.

### Why F1 is the default for IR

We considered three rubric shapes during design:

* **A. F1** — penalises over-shipping AND missing. `oracle_overlap_f1`.
* **B. F-beta with β > 1** — recall-tilted but still penalises over-
  shipping at scale. Rejected: introduces a hidden parameter that
  reviewers have to remember; the e5d7a4e7 acceptance case (β=2 →
  F2 ≈ 0.59) lands above the pass threshold which contradicts the
  "didn't solve" framing.
* **C. recall-only** — only missing costs. `oracle_overlap_recall`.

**A is the default** because:

* It penalises the adversarial-dump pathology (recall=1.0,
  precision≈0.02 → F1≈0.04) at the headline level. Reviewers don't have
  to dig into IR diagnostics to spot "this score is fake."
* It's the conservative choice — symbol-reference-trace tasks
  *typically* care about both finding the truth and not shipping noise,
  and the cost of mis-routing one to recall-only is "an agent that
  dumps the repo wins" (loud-failure). Mis-routing the other way costs
  "a discovery task agent has to be more selective" (quiet false-fail
  but reviewers can spot it via `sub_scores.recall`).
* Per-task opt-in to `oracle_overlap_recall` is one line of metadata;
  no global fork is needed.

The default applies to the on-disk continuous oracle
(`mining/writer.py:_ORACLE_PY` writes F1 / weighted F1 to `reward.txt`)
and to the in-process `score_file_list` / `score_symbol_list`. The
existing oracle script writes F1 today — we just stopped throwing it
away in favour of recall.

### Known limitation: symbol normalization discards module paths

`score_symbol_list` normalizes both sides through
`core/scoring.py:_normalize_symbol`, which strips everything before the
last `.` / `::` separator and lowercases — `foo.bar.MyClass` and
`baz.MyClass` both normalize to `myclass` and **count as a match**. This
buys tolerance to agents that report bare names where the oracle stores
fully-qualified ones (and vice versa), at the cost of false-positive
credit when two genuinely different symbols share a leaf name. The same
trade applies to `_normalize_path` stripping `/workspace/`-style
prefixes. For oracles where leaf-name collisions are plausible (common
names like `run`, `Config`, `Handler` across modules), prefer a
file-list oracle or an `oracle_checks` rubric that pins paths. Making
the stripping behaviour per-task-configurable is open follow-up work.

### `oracle_checks` — structured-rubric criteria

`oracle_checks` is the structured-rubric scorer family. Use it for tasks
where the verifier evaluates a list of named criteria — "did the diff
handle edge case X?", "are all error-handling branches tested?" — that
do not collapse to file/symbol overlap. Mirrors the CSB `oracle_checks`
pattern so the same family name lines up across rigs.

Per-criterion verifiers run independently under the standard scoring
sandbox and emit `{"score": 0.0-1.0, "passed": bool}` JSON on stdout
(or fall back to exit code: `0` → `1.0`, nonzero → `0.0`). The headline
reward is the **weight-normalized average** `Σ(weight_i · score_i) /
Σ(weight_i)`, so weights need not sum to `1.0` — a rubric with weights
`[2, 1, 1]` is equivalent to `[0.5, 0.25, 0.25]`.

Rubric source resolution order:

1. `metadata_criteria` constructor argument — populated from `task.toml`
   `[[rubric_criteria]]` by the task loader.
2. `tests/rubric.json` on disk — for tasks that ship the rubric inline.

#### `task.toml` schema

Declare the rubric inline alongside `[verification]`:

```toml
[verification]
type = "oracle_checks"
reward_type = "oracle_checks"
verification_mode = "test_script"

[[rubric_criteria]]
name = "handles_edge_case_x"
weight = 0.5
verifier = "check_edge_case.sh"
description = "Verify the agent's diff handles the empty-input edge case"

[[rubric_criteria]]
name = "covers_error_branches"
weight = 0.3
verifier = "check_error_branches.sh"

[[rubric_criteria]]
name = "preserves_public_api"
weight = 0.2
verifier = "check_public_api.sh"
```

Required fields per criterion: `name`, `weight`, `verifier`. `description`
is optional and recorded in metadata only — the scorer ignores it.

#### `tests/rubric.json` schema

For tasks that ship the rubric on disk (the format the scorer reads when
no `metadata_criteria` are passed), `tests/rubric.json` is a JSON list:

```json
[
  {
    "name": "handles_edge_case_x",
    "weight": 0.5,
    "verifier": "check_edge_case.sh",
    "description": "Verify the agent's diff handles the empty-input edge case"
  },
  {
    "name": "covers_error_branches",
    "weight": 0.3,
    "verifier": "check_error_branches.sh"
  }
]
```

Verifier scripts live in `tests/verifiers/` — the same layout that
`CheckpointScorer` uses. The sandbox copies the task directory, runs
each verifier with `AGENT_OUTPUT` set to the captured agent text, and
collects the JSON / exit code result.

#### `sub_scores` shape

```jsonc
{
  "scorer_family": "oracle_checks",
  "sub_scores": {
    "composite": 0.65,
    "criterion_scores": {
      "handles_edge_case_x": 1.0,
      "covers_error_branches": 0.5,
      "preserves_public_api": 0.0
    },
    "total_weight": 1.0
  }
}
```

`details.criterion_scores` and `details.criterion_weights` mirror this
breakdown for `aggregate.json` consumers that read `scoring_details`
directly.

#### Composing with `dual_composite`

`oracle_checks` is a regular `Scorer`; `DualScorer` can blend it with a
direct (binary / continuous) leg the same way it blends `ArtifactScorer`.
Tasks that want "test.sh must pass AND the rubric criteria must pass"
declare:

```toml
[verification]
verification_mode = "dual"
reward_type = "binary"        # direct leg: tests/test.sh
scoring_policy = "gate"        # 1.0 only when both legs passed
# (artifact leg is OracleChecksScorer if the task ships a rubric)
```

The current `DualScorer` pairs the direct leg with `ArtifactScorer` —
swapping in `OracleChecksScorer` for tasks that prefer rubric criteria
over `ground_truth.json` answer-typing is a follow-up wiring change
that doesn't require changes to the family contract.

## What gets emitted

### `ScoreResult`

```python
@dataclass(frozen=True)
class ScoreResult:
    score: float                           # = reward (canonical headline)
    passed: bool                           # back-compat (>= PASS_THRESHOLD for IR)
    error: str | None = None
    details: dict = ...                    # back-compat: precision/recall/f1/etc.
    reward_score: float | None             # mirrors score
    ir_metrics: dict                       # back-compat IR view: {precision, recall, f1, weighted_recall?}
    scorer_family: str = ""                # NEW: declared rubric
    sub_scores: dict = ...                 # NEW: rubric breakdown {recall, precision, f1, reward, ...}
    diagnostics: dict = ...                # NEW: {"ir_metrics": {...}}
```

`details` continues to carry `precision`/`recall`/`f1` so older code
that reads `scoring_details["f1"]` keeps working. `sub_scores` is the
canonical source for the rubric breakdown going forward.

### `completed.json#scoring_details` (per task)

```jsonc
{
  "passed": false,
  "error": null,
  // back-compat IR fields (still populated)
  "precision": 0.2571,
  "recall": 1.0,
  "f1": 0.4092,
  // NEW: declared rubric
  "scorer_family": "oracle_overlap_f1",
  // NEW: rubric breakdown
  "sub_scores": {
    "precision": 0.2571,
    "recall":    1.0,
    "f1":        0.4092,
    "reward":    0.4092
  }
}
```

### `scoring.json#diagnostics` — executor-injected run telemetry

The executor (`core/executor.py:_save_task_artifacts`) merges per-trial
runtime telemetry into the serialised `scoring_details.diagnostics`
block at scoring.json write time so the per-task contract is
self-contained without forcing the scorer to know about run-level
metadata. The full shape:

```jsonc
{
  "diagnostics": {
    "task_time_seconds":    18.4,    // wall-clock duration
    "token_cost_usd":       0.0034,  // adapter-computed total cost (cache-aware)
    "input_tokens":         320,     // uncached input portion only
    "cache_read_tokens":    98765,   // bulk re-use; billed at the reduced cache-read rate
    "cache_creation_tokens": 4321,   // write-through to cache; billed at the higher cache-write rate
    "output_tokens":        567,
    "ir_metrics": { "precision": …, "recall": …, "f1": … }  // when present
  }
}
```

Field semantics — important for cache-aware cost-Pareto plots:

* **`input_tokens`** — *uncached* prompt tokens only. With heavy prompt
  caching this can be tiny (tens to hundreds) for long sessions where
  most context is reused. Reading it as "the model saw N input tokens"
  is wrong; it's "N tokens of fresh input were billed at the standard
  rate."
* **`cache_read_tokens`** — bulk re-use. The Anthropic SDK reports this
  separately as `cache_read_input_tokens`. For multi-turn sessions this
  is typically the dominant token category by raw count.
* **`cache_creation_tokens`** — write-through cost. Billed at a higher
  rate than uncached input (cache-write rate). Reported by the SDK as
  `cache_creation_input_tokens`.
* **`token_cost_usd`** — already accounts for the full mix at the
  correct per-category rate. The raw counts are surfaced so cost-Pareto
  plots can reason about cache-hit rates without re-deriving cost.

All four token fields are omitted (not zero-filled) when the adapter
couldn't capture telemetry, so callers can distinguish "agent reported
zero" from "adapter didn't record this category."

### `aggregate.json#config_summaries[label]`

```jsonc
{
  "tasks_completed": 80,
  "mean_automated_score": 0.41,    // headline reward
  "mean_reward": 0.41,             // alias for clarity
  "stdev_automated_score": 0.18,
  "total_cost_usd": 4.20,
  "mean_cost_per_task": 0.05,
  "score_per_dollar": 9.8,

  // raw token counts — sum/mean across tasks that reported usage. Tasks
  // where the adapter couldn't capture telemetry are excluded from the
  // mean rather than counted as zero. cost_usd already accounts for the
  // mix at the correct per-category rate.
  "total_input_tokens":            25600,
  "total_output_tokens":            45360,
  "total_cache_read_tokens":      7901200,  // dominant for multi-turn sessions
  "total_cache_creation_tokens":   345680,
  "mean_input_tokens_per_task":      320.0,
  "mean_output_tokens_per_task":     567.0,
  "mean_cache_read_tokens_per_task": 98765.0,
  "mean_cache_creation_tokens_per_task": 4321.0,

  // back-compat: kept at the top level so older consumers don't break
  "mean_precision": 0.26,
  "mean_recall":    1.0,
  "mean_f1":        0.41,

  // canonical IR view going forward
  "ir_diagnostics": {
    "mean_precision": 0.26,
    "mean_recall":    1.0,
    "mean_f1":        0.41
  },

  // NEW: which rubric produced each task's reward
  "scorer_family_distribution": {
    "oracle_overlap_f1": 78,
    "oracle_overlap_recall": 2
  }
}
```

A config that mixes families is immediately visible — and because the
flat `mean_recall` is still computed across every IR-family task, you
can compare it against the headline reward to see how much room
recall-tilted tasks could buy.

## Reward population — infra-failure exclusion (codeprobe-77z)

The headline reward (`mean_score` / `median` / `pass_rate` / CIs) is computed
over the **valid** trials only. An infra casualty — a trial that crashed on
infrastructure rather than producing a measurement (output-token-ceiling
overrun, quota/OAuth exhaustion, rate limit, network/timeout, MCP connect
failure, crash) — is stamped `automated_score=0.0` by the executor, but that
`0.0` is not a solution-quality signal and is **excluded** from the reward
population by `stats.is_scorable_run`. The count is surfaced, never silent:

- `ConfigSummary.infra_failure_count` — per-arm infra casualties (a superset of
  `quota_error_count`, which stays the quota-specific sub-count for the
  codeprobe-9xrl contract; both are a subset of `errored_count`). Excluded
  trials stay in the structural totals (`total_tasks` / `errored` / cost /
  tokens).
- `Report.validity` — a run-level `ValidityReport`. It **FAILs** while any
  unresolved infra casualty remains and lists the offending trial ids; the run
  is not "quotable/complete" until those trials are re-run to `completed` (or
  reclassified genuine with a reason). `codeprobe interpret` renders the verdict
  in text/JSON, lifts `validity` to the envelope top level, and **exits 2** on a
  FAIL (`error.code = VALIDITY_FAILED`) so a pipeline branching on the exit code
  cannot read an invalid run as a success.

Classification is structural/string-only (ZFC-clean) — a genuinely low score is
a real data point and is never reclassified as infra, and a terminal
`error_max_turns` failure stays a real `0.0` rather than being re-run into the
same cap. Full playbook:
[`docs/conventions/validity-triage.md`](conventions/validity-triage.md).

## Routing — how the family is chosen

`ContinuousScorer` and `ArtifactScorer` resolve the family at score
time via `_select_ir_family`:

1. **Explicit override:** `metadata.json` carries
   `verification.scorer_family = "..."`. If present, it wins. Mining
   writers MAY populate this when emitting tasks; missing key is fine.
2. **Weighted oracle:** if the on-disk `metrics.json` reports a finite
   `weighted_recall`, the family becomes `oracle_weighted_f1`.
3. **Default:** `oracle_overlap_f1`.

Non-IR scorers declare their family at the class level
(`BinaryScorer.SCORER_FAMILY = "binary_test"`, etc.) and ignore
metadata routing.

In-process API callers can pass a `family=` kwarg directly to
`score_file_list` / `score_symbol_list` for tests and ad-hoc scoring
outside the artifact-scorer flow.

## Bias detection — informational over-shipping (preserved)

`detect_overshipping_anti_pattern` keeps its informational role.
Triggers: same task scored by two configs, both with recall ≥ 0.95, one
with precision ≤ 0.5, precision delta ≥ 0.3. Under the new default
family the F1 reward already captures the precision penalty, so the
warning is redundant for `oracle_overlap_f1` configs — it's still
emitted as informational because it's useful when comparing a
default-family config against an `oracle_overlap_recall` config (same
recall, very different precision, very different reward).

## Bias detection — backend_overlap severity (unchanged)

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
  `backend_overlap_informational` for the informational variant.

## Reading scoring outputs in practice

* **Ranking configs?** Use `mean_reward` (or its alias
  `mean_automated_score`).
* **Diagnosing why a config wins or loses?** Look at the per-task
  `scoring_details.sub_scores` — the rubric breakdown shows whether the
  loss came from low precision (over-shipping) or low recall
  (under-shipping). Aggregate-level `ir_diagnostics.mean_precision` vs
  `ir_diagnostics.mean_recall` tells the same story across the whole
  run.
* **Auditing a single task?** Read `scoring_details.scorer_family` to
  know which rubric scored it, then `scoring_details.sub_scores` for
  the inputs.
* **Comparing two configs?** Check
  `scorer_family_distribution` — if the two configs scored under
  different families, the headline reward isn't directly comparable
  without knowing how many tasks landed in each family.
* **Investigating an over-shipping pattern?** Look for `overshipping`
  warnings in `aggregate.json#bias_warnings`. Under the new default
  family the warning is informational; the F1 reward already reflects
  the penalty.

## Worked example: e5d7a4e7 + 38223444 under the new default

Two acceptance cases from the codeprobe-ur8d N=3 repeat run that exposed
the original recall-only bug:

| Task        | family             | precision | recall | reward (F1) | sub_scores       |
| ----------- | ------------------ | --------: | -----: | ----------: | ---------------- |
| e5d7a4e7    | oracle_overlap_f1  | 0.26      | 1.00   | **0.41**    | `{p:0.26, r:1.0, f1:0.41, reward:0.41}` |
| 38223444    | oracle_overlap_f1  | 1.00      | 0.33   | **0.50**    | `{p:1.0, r:0.33, f1:0.50, reward:0.50}` |

* **A3 satisfied** — e5d7a4e7's adversarial dump (the agent shipped
  ≈300 files including all 80 in the answer key) scores 0.41, below
  the 0.5 pass threshold. The reward correctly says "didn't solve"
  even though recall is 1.0.
* **A4 satisfied** — 38223444's "found 1/3 with precision=1.0" agent
  scores 0.5, exactly at the threshold. The reward correctly captures
  "found a tight slice but missed two-thirds." Under the original
  recall-only contract this would have been 0.33 (overstated as
  "shipped few but not enough"); under the F1 default the precision
  reward credits the agent for not over-shipping and the recall
  penalty captures the missed truth.

Reviewers who want a recall-tilted view of either task add
`{"verification": {"scorer_family": "oracle_overlap_recall"}}` to that
task's metadata and re-score. Both views live alongside in
`scoring_details.sub_scores`.

## Verifier-honesty lint

`tests/lint/test_scorer_honesty.py` is a pytest-based AST lint over
`core/scoring.py` and `core/bias_detection.py`. It catches four
classes of *verifier dishonesty* and runs as part of the standard
`pytest` invocation (no separate CLI step):

* **`missing-scorer-family`** — every `ScoreResult(...)` must declare
  `scorer_family=`. Empty strings are allowed (the field is opaque
  in that case); the kwarg has to be present.
* **`quiet-recall-fallback`** — F1-family branches that fall back to
  `reward = recall` / `weighted_recall`. The voxa-class regression.
* **`hardcoded-threshold`** — inline float literals in compares AND
  module-level threshold-named constants that are not config-plumbed.
* **`bare-except`** — `except:` / `except Exception:` without a
  `# noqa` annotation in scorer code.

### Adding a new scorer family

1. Add the name to `SCORER_FAMILIES` in `core/scoring.py`.
2. Document the rubric and `sub_scores` shape in this file.
3. Make sure every `ScoreResult` your scorer emits passes the lint
   — declare `scorer_family=` on success AND error paths.
4. Add a fixture-backed test under `tests/test_scoring_reward.py`.

Pre-existing offenders are tracked in `_KNOWN_OFFENDERS` in the lint
file with explicit follow-up bead IDs. Adding a new entry needs
reviewer sign-off; deleting an entry once the offender is fixed is
mandatory (the `test_scorer_honesty_known_offenders_still_present`
test flags stale entries).

## Verifier materialization — verdict + materialized_via

> Slice 1b of bead `codeprobe-xysn` (2026-05). Pairs with the
> `verdict` and `materialized_via` fields landed in Slice 1a
> (commit `e6c312c`).

The headline `score` answers "did the agent solve it?", but two
distinct failure modes used to collapse onto the same `score=0.0`
result: an *agent failure* (test.sh exited non-zero because the
agent shipped the wrong answer) and a *verifier-infrastructure
failure* (the harness couldn't honestly run the test against the
agent's output, e.g. a malformed diff that `git apply --check`
rejected). The premortem flagged this collision as the most
damaging silent failure for hosted-agent comparisons (Theme C,
risk R1). Slice 1b separates them via the `verdict` field.

### The 5-state outcome table

| apply_check | test.sh   | verdict          | materialized_via | what happened                                          |
| ----------- | --------- | ---------------- | ---------------- | ------------------------------------------------------ |
| ok          | exit 0    | `correct`        | `git_apply`      | diff applied cleanly; test passed                      |
| ok          | exit ≠ 0  | `incorrect`      | `git_apply`      | diff applied; test rejected the agent's answer         |
| ok, empty   | exit 0    | `correct`        | `git_apply`      | agent shipped no change; verifier ran on pristine base |
| failed      | (skipped) | `verifier_error` | `git_apply`      | apply rejected; agent NOT graded                       |
| n/a         | exit 0/≠0 | `correct/incorrect` | `in_place`    | legacy path — workspace not a git repo, or task multi-repo / scaffold-isolated |

`materialized_via` records *how* the verifier got at the agent's
final state:

* `git_apply` — fresh `git clone --local --no-hardlinks` at the
  executor-captured `base_commit`, then the agent's full diff
  (committed + staged + unstaged + untracked) applied via
  `git apply --binary`. The verifier sees an isolated tree.
* `in_place` — legacy behaviour: the verifier reads the agent's
  worktree directly. Used as a fallback when `base_commit` capture
  was not eligible (no `.git`, multi-repo, scaffold/hide source).
* `file_overlay` — reserved for vendor adapters that return raw
  file blobs rather than a unified diff (not used in Slice 1b).

### When `git_apply` is used

The executor (`core/executor.py`) captures
`base_commit = git rev-parse HEAD` of the agent's effective
workspace BEFORE `adapter.run` and threads it into
`BinaryScorer.score(... agent_state=...)` when ALL of these hold:

1. The agent's effective workspace is a single-repo checkout
   (no `metadata.additional_repos`).
2. `hide_local_source == "off"` — scaffold/hide modes mutate the
   workspace and would pollute the diff with overlay artefacts.
3. The workspace has a `.git` directory.
4. `git rev-parse HEAD` succeeds.

If any condition fails, the scorer falls through to `in_place`.
This is silently consistent with legacy behaviour for non-git
workspaces and IR / discovery tasks that operate against an
extracted corpus.

### Diff capture is non-destructive

`_capture_workspace_diff` uses a private `GIT_INDEX_FILE`
tempfile so `git add -A` never touches the agent's real index.
After scoring, the workspace's porcelain output is identical to
what the agent left behind. This matters for retries, dual
scorers, and human follow-up inspection.

### Limits

* Diff is capped at `_MAX_DIFF_BYTES = 100 MiB`. Larger diffs
  route to `verifier_error` to avoid pinning multiple GB of
  patch bytes in memory.
* Every git subprocess has a `_GIT_TIMEOUT_SECONDS = 60` ceiling.
* `BinaryScorer` is the only scorer that consumes `agent_state`
  in Slice 1b. `ContinuousScorer`, `CheckpointScorer`,
  `OracleChecksScorer`, and `DualScorer`'s direct/artifact legs
  keep the legacy `in_place` path. Extending `git_apply` to the
  dual direct-leg lands in a follow-up slice.

### What downstream consumers see

* `ScoreResult.verdict` — `correct` / `incorrect` /
  `verifier_error` / `inconclusive` / `None` (legacy / unmigrated).
* `ScoreResult.materialized_via` — `in_place` / `git_apply` /
  `file_overlay`.
* `completed.json#scoring_details["verdict"]` — same value.
* `completed.json#scoring_details["materialized_via"]` — same.

Aggregate consumers that want to distinguish agent failure from
verifier failure should branch on `verdict`, not just `score`.

## Turn cap

`--max-turns` bounds how many turns the agent may take per task. It is a
resource budget (like the per-task timeout and `--max-cost-usd`), not a
scoring knob — but it interacts with reward, so it lives here.

`codeprobe-aupz` found a single global cap is the wrong shape: a
`--max-turns=50` is harmless for **oracle_checks** (those finish in <10
turns) but collapses **SDLC** reward — 87% of SDLC trials hit
`error_max_turns`. The 4cl6 retune then showed *every* finite SDLC cap
depresses reward, so SDLC is left uncapped.

### Resolution precedence

Resolved per trial in `core/turn_cap.py` (`resolve_turn_cap`), highest
priority first:

1. **CLI** `--max-turns` — explicit global override, always wins.
2. **experiment.json** `max_turns` on the config — the experiment
   author's explicit per-run cap.
3. **task** `max_turns_override` — optional `int` in `task.toml` /
   `metadata.json` under `metadata.max_turns_override`.
4. **family default** — `core.turn_cap.FAMILY_DEFAULT_MAX_TURNS`:

   | family          | default cap                       |
   | --------------- | --------------------------------- |
   | `sdlc`          | `None` (**uncapped**)             |
   | `oracle_checks` | `50`                              |
   | _anything else_ | `75` (`DEFAULT_FAMILY_MAX_TURNS`) |

The family is read mechanically from existing task metadata —
`verification.type` / `verification.reward_type == "oracle_checks"`, else
`metadata.category` — by `resolve_turn_cap_family`. No heuristic
classification (ZFC-safe); the author set these fields at mine time.

Rungs 1–2 fold into the same "explicit config cap" tier inside the
resolver: the executor receives the already-resolved config value plus a
`config_max_turns_source` of `"cli"` or `"experiment"`.

### Telemetry

Every trial envelope (`completed.json#metadata`, on success **and** on the
`error_max_turns` path) carries:

* `max_turns_chosen` — the resolved cap (`int` or `null` when uncapped).
* `max_turns_source` — `cli` / `experiment` / `task` / `family_default`.

Cap-retune analysis reads these directly to confirm which cap each trial
actually ran under instead of inferring it from the run command.

## Out of scope

* The on-disk oracle script (`mining/writer.py:_ORACLE_PY`) writes
  `reward.txt = f1` (or `weighted_f1`) — same as before. The runner-
  side family routing decides which value becomes the headline reward
  without re-mining tasks.
* Cost/time live on `CompletedTask` and are mirrored into the
  serialised `scoring.json#diagnostics` block at write time (see
  *`scoring.json#diagnostics` — executor-injected run telemetry*
  above). The `diagnostics` field on a live `ScoreResult` still
  carries scorer-side observations only (currently the IR metrics
  view); the executor merges in `task_time_seconds`,
  `token_cost_usd`, and the four token-count fields when serialising.
* `pass` thresholds are preserved on `ScoreResult.passed` for back-
  compat — `score_passed` in `analysis/stats.py` reads it. New
  consumers should compare `score` to a context-specific threshold
  rather than read this flag.
