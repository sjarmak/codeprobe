# codeprobe `benchmark_qa_core` corpus run — 2026-04-30

## Summary

Ran `verify_task_qa` (the codeprobe-side adapter for `benchmark_qa_core`)
across the canonical corpus on branch
`feature/codeprobe-ceuu-benchmark-qa-core`:

| corpus root              | tasks |
|--------------------------|-------|
| `examples/dual/**`       | 21    |
| `.codeprobe/tasks/**`    | 2     |
| `e2e-codeprobe-self/**`  | 4     |
| `tests/fixtures/**`      | 1     |
| **total**                | **28** |

Results:

| pass | with errors | with warnings |
|-----:|------------:|--------------:|
|   28 |           0 |             0 |

(With the corpus script's `repo_root` heuristic that maps mined tasks
back to the actual codeprobe repo. See "Caller-config gap" below for
what happens without it.)

## Reproduction

```bash
python scripts/run_qa_corpus.py            # default — gives 0 errors
python scripts/run_qa_corpus.py --no-repo-root-hint  # surfaces the
                                            # caller-config gap
```

## Findings histogram (clean run)

No findings produced — every task in the canonical corpus passes the
three checks (`check_oracle_coherence`, `check_scoring_honesty`,
`check_aux_file_leakage`) with the codeprobe default tier table and
Class-D suppression on.

## Caller-config gap (default `repo_root`)

The orchestrator defaults `repo_root` to `task_dir` itself — that's the
right answer for synthetic example tasks that bundle their oracle files
inside the task directory, but it's wrong for mined tasks whose oracle
files live in the original repo. Running the same corpus with
`--no-repo-root-hint` surfaces the gap:

| pass | with errors | error finding code |
|-----:|------------:|---------------------|
|   23 |           5 | A1 (×91)            |

Every error is `A1: Oracle file does not exist: <repo-relative-path>`,
because the orchestrator was looking for the file under the task
sandbox rather than the codeprobe source tree.

This is a UX issue, not a contract violation — the lib is honest about
what it found. But every adapter caller will trip over it the first
time they wire mined tasks. **Proposed follow-up:** auto-detect by
reading the metadata `repo` / `repo_path` field and resolving relative
to the codeprobe repo root when present, with an explicit override
parameter for callers that want the task-local check (e.g. dual tasks
that bundle a tiny synthetic repo).

Filed as bead `codeprobe-qa-repo-root-resolve` (see "Follow-ups" below).

## What changed in this PR

* **`src/codeprobe/qa/verify.py`** — new module. Exposes
  `verify_task_qa(task_dir, *, repo_root=None, …) -> QAVerifyResult`.
  Reads `task.toml` / `metadata.json` (flattened across sections so
  nested keys like `[verification]` are visible to the lib),
  `tests/ground_truth.json` (v2 `checks`, v1 `answer_type`, legacy
  `expected`), and runs all three checks. `D1` / `D2` findings are
  suppressed by default — codeprobe's scoring contract already weights
  off-scope answers via reward.
* **`src/codeprobe/qa/__init__.py`** — re-exports `verify_task_qa`,
  `QAVerifyResult`, `load_task_meta`.
* **`src/codeprobe/cli/validate_cmd.py`** — `validate` learned a `--qa`
  flag that runs the lib and folds findings into the existing check
  output (errors fail, warnings/info pass with a labelled detail).
* **`src/codeprobe/core/scoring.py`** — registry adds
  `oracle_overlap_fbeta` (F-beta with per-task `verification.fbeta_beta`,
  defaulting to 1.0 ≡ F1). `ScoreResult` gains a `reward` field that
  mirrors `score` so the codeprobe / EB / CSB unified contract field
  name is available without breaking legacy callers. F-beta is wired
  through `score_file_list`, `score_symbol_list`, `ArtifactScorer`
  (v2/v1/legacy paths), and `ContinuousScorer._derive_reward_and_metrics`.
* **`src/codeprobe/core/executor.py`** — `_save_task_artifacts` now
  emits the unified contract in `scoring.json`: top-level `reward`
  (mirrors `score`) plus a `diagnostics` block carrying
  `task_time_seconds`, `token_cost_usd` (when available), and any
  `ir_metrics` the scorer populated.
* **Tests** — `tests/test_scoring_unified_contract.py` (21 tests:
  `reward` mirroring, F-beta family, executor diagnostics contract);
  `tests/test_qa_verify.py` (17 tests: load_task_meta TOML+JSON,
  oracle file checks, Class-D suppression, scoring honesty E1/E2,
  leakage F2, default tier table, QAVerifyResult flags).

## Test command + results

```bash
python -m pytest \
  tests/test_scoring.py tests/test_scoring_v2.py \
  tests/test_scoring_extended.py tests/test_scoring_reward.py \
  tests/test_scoring_unified_contract.py tests/test_qa_verify.py \
  src/codeprobe/qa/benchmark_qa_core/tests \
  -q
```

Result: **214 passed in 2.81s**.

## scorer_family registry contents

| family                    | reward formula                               | typical use                                  |
|---------------------------|----------------------------------------------|----------------------------------------------|
| `oracle_overlap_f1`       | F1(precision, recall)                        | symbol-reference-trace (default)             |
| `oracle_overlap_fbeta`    | F-beta(precision, recall; β from metadata)   | over-shipping-sensitive tasks; β<1.0 favours precision |
| `oracle_overlap_recall`   | recall                                       | file-discovery / triage                      |
| `oracle_weighted_f1`      | weighted F1 from on-disk oracle              | org-scale tier-weighted                      |
| `oracle_weighted_recall`  | weighted recall                              | tier-weighted recall family (legacy default) |
| `sequence_lcs`            | LCS / max(len(expected), len(actual))        | dependency_chain                             |
| `exact_match`             | int(expected == actual)                      | count, boolean, text                         |
| `binary_test`             | int(test.sh exit == 0)                       | direct-leg test_script                       |
| `continuous`              | clamp(reward.txt or stdout last line, 0, 1)  | direct-leg continuous reward                 |
| `weighted_checkpoints`    | Σ weight × verifier_score                    | CheckpointScorer / v2 multi-check composite  |
| `dual_composite`          | policy(direct, artifact)                     | DualScorer                                   |

The bead's required minimum (`oracle_overlap_recall`,
`oracle_overlap_fbeta`, `continuous`) is covered.

## ScoreResult schema sample (one per family touched in this PR)

### `oracle_overlap_fbeta` (β = 0.5)
```json
{
  "score": 0.301,
  "reward": 0.301,
  "passed": false,
  "scorer_family": "oracle_overlap_fbeta",
  "sub_scores": {
    "precision": 0.2,
    "recall": 1.0,
    "f1": 0.333,
    "reward": 0.301,
    "fbeta_beta": 0.5
  },
  "ir_metrics": {"precision": 0.2, "recall": 1.0, "f1": 0.333},
  "diagnostics": {
    "ir_metrics": {"precision": 0.2, "recall": 1.0, "f1": 0.333},
    "task_time_seconds": 12.5,
    "token_cost_usd": 0.0034
  }
}
```

### `oracle_overlap_recall`
```json
{
  "score": 1.0,
  "reward": 1.0,
  "passed": true,
  "scorer_family": "oracle_overlap_recall",
  "sub_scores": {
    "precision": 0.5,
    "recall": 1.0,
    "f1": 0.667,
    "reward": 1.0
  },
  "ir_metrics": {"precision": 0.5, "recall": 1.0, "f1": 0.667},
  "diagnostics": {"ir_metrics": {"precision": 0.5, "recall": 1.0, "f1": 0.667}}
}
```

### `continuous` (no IR metrics)
```json
{
  "score": 0.83,
  "reward": 0.83,
  "passed": true,
  "scorer_family": "continuous",
  "sub_scores": {"raw_score": 0.83},
  "diagnostics": {"task_time_seconds": 4.1}
}
```

## Follow-ups

* **Auto-resolve `repo_root` from task metadata** — see "Caller-config
  gap" above. Without this, every adapter caller hits the same A1
  false-positive on first wiring. Proposed bead title:
  `codeprobe-qa-repo-root-resolve — auto-detect repo_root from
  task.metadata.repo / metadata.repo_path; fall back to task_dir`.
* **Surface QA findings in the `aggregate.json` writer** — currently
  `verify_task_qa` is plumbed only through `validate --qa`. Once the
  acceptance loop is happy with the contract, fold a per-task QA pass
  into `_save_task_artifacts` so every run captures schema-level bugs
  alongside the scoring artefact. This is independent of the dr-2vydrm
  thread and can wait until codeprobe-ssf prioritises it.

## Local commit

The work is on branch `feature/codeprobe-ceuu-benchmark-qa-core`. Commit
SHA is recorded by the parent dr-2vydrm.4 mirror when this proxy bead
closes.
