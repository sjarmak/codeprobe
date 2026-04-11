● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ 1 line read

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ 1 line read

● Read writer.py
  │ src/codeprobe/mining/writer.py
  └ 1 line read

● Read prd_dual_verifier_mining.md
  └ 199 lines read

● Read registry.py
  │ src/codeprobe/core/registry.py
  └ 114 lines read

● Read task.py
  │ src/codeprobe/models/task.py
  └ 150 lines read

● Read experiment.py
  │ src/codeprobe/models/experiment.py
  └ 74 lines read

● Read stats.py
  │ src/codeprobe/analysis/stats.py
  └ 455 lines read

● Read __init__.py
  │ src/codeprobe/loaders/__init__.py
  └ 161 lines read

● Read validate_cmd.py
  │ src/codeprobe/cli/validate_cmd.py
  └ 288 lines read

Now let me read the large files in sections:

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L1:100 (100 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L1:100 (100 lines read)

● Read writer.py
  │ src/codeprobe/mining/writer.py
  └ L1:100 (100 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L100:300 (201 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L100:280 (181 lines read)

● Read writer.py
  │ src/codeprobe/mining/writer.py
  └ L100:300 (201 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L300:500 (201 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L280:450 (171 lines read)

● Read writer.py
  │ src/codeprobe/mining/writer.py
  └ L300:500 (201 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L450:600 (151 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L500:650 (151 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L650:700 (26 lines read)

● Read writer.py
  │ src/codeprobe/mining/writer.py
  └ L500:700 (111 lines read)

Now I have enough to write the full review. Let me compile it.

● Read __init__.py
  │ src/codeprobe/loaders/__init__.py
  └ L96:105 (9 lines read)

---

## Feasibility Review: PRD Dual Verifier Mining — Phase 1 Must-Haves

---

### R1 — `DualScorer` class in `core/scoring.py`

**Implementable: Yes, with one non-trivial wiring problem.**

The Scorer protocol signature is `score(agent_output, task_dir) → ScoreResult`. `DualScorer` must run `BinaryScorer` (or `ContinuousScorer`) and `ArtifactScorer` independently and populate `scoring_details`. But `ScoreResult` only has `score`, `passed`, `error` — no `scoring_details` field. The executor (executor.py:400–403) hardcodes:

```python
scoring_details={"passed": score_result.passed, "error": score_result.error}
```

Sub-scores from `DualScorer` have no channel back to `CompletedTask.scoring_details` unless `ScoreResult` gains a new field (e.g., `scoring_details: dict = field(default_factory=dict)`) or the executor special-cases `DualScorer`.

**Files:** `core/scoring.py`, `core/executor.py`  
**Estimated LOC:** ~80 (DualScorer class) + ~25 (executor wiring)  
**Gotcha:** `ScoreResult` must be extended. If done wrong, every existing test that constructs `ScoreResult` positionally will break.

---

### R2 — Registry entry for `"dual"` in `core/registry.py`

**Implementable: Trivially.**

One line added to `_SCORER_BUILTINS`:
```python
"dual": "codeprobe.core.scoring:DualScorer",
```
`VALID_REWARD_TYPES` (scoring.py:619) is dynamically computed from `available_scorers()`, so it will automatically include `"dual"` once the entry is added.

**Secondary gotcha:** `get_scorer()` return type annotation (line 624) lists 4 concrete classes — needs `| DualScorer` appended.

**Files:** `core/registry.py`, `core/scoring.py` (annotation only)  
**Estimated LOC:** ~5

---

### R3 — Optional `scoring_policy` in task.toml

**Implementable: Yes, but with an architectural friction point.**

`DualScorer.score(agent_output, task_dir)` has no parameter for policy. The only clean paths to get `scoring_policy` at score time are:

1. Read `task_dir/metadata.json` inside `score()` — disk IO inside scorer, but executor already does this pattern
2. Construct `DualScorer(policy="min")` — but `resolve_scorer("dual")` calls `cls()` with zero args (registry.py:32)

Option 1 is the path of least resistance. Option 2 requires the executor to bypass `get_scorer()` and construct `DualScorer` directly after reading metadata. **Option 2 is cleaner architecturally but requires executor changes and makes the registry less useful for dual tasks.**

Additionally: `loaders/__init__.py:_build_task()` only populates 4 fields of `TaskVerification` (type, command, reward_type, checkpoints). Fields `verification_mode` and new `scoring_policy` are never loaded into the `Task` model — only readable from raw `metadata.json`. The executor already works around this by reading raw JSON directly (executor.py:202–205). The loader gap is a pre-existing inconsistency that this PRD doesn't fix but must not make worse.

**Files:** `core/scoring.py` (DualScorer policy logic), `models/task.py` (add `scoring_policy` to `TaskVerification`), `loaders/__init__.py` (populate new field), `core/executor.py` (pass policy to DualScorer)  
**Estimated LOC:** ~40

---

### R4 — Writer emits dual-verification task layout

**Implementable: Yes, but Phase 1 scope is unclear.**

`write_task_dir()` dispatches on `task.verification.type == "oracle"` for org-scale tasks and falls through to test_script layout for everything else. For dual tasks, it needs to emit both `tests/test.sh` AND `tests/ground_truth.json`.

The content for `ground_truth.json` must come from somewhere. For manually curated tasks (Phase 1), the Task model already has `oracle_answer: tuple[str, ...]` and `oracle_tiers` in `TaskVerification`. So a new `"dual"` branch in `write_task_dir()` could write both files. The test.sh for dual tasks needs to run both `bash tests/test.sh` (direct) AND signal that ArtifactScorer should run — but that's the executor's job, not test.sh's.

**Gotcha:** The PRD acceptance criterion says "Both `codeprobe validate` and `codeprobe run` succeed" — this works. But the instruction.md template for dual tasks also needs to tell agents to produce `answer.json` (R14, a Should-Have, not enforced here). **Risk: dual tasks written without the instruction update will silently fail the artifact leg every time.**

**Files:** `mining/writer.py`  
**Estimated LOC:** ~50

---

### R5 — Executor auto-selects `DualScorer` for `verification_mode="dual"`

**Implementable: Yes.**

Executor already reads raw metadata JSON and auto-detects `reward_type` (executor.py:202–205). Same pattern can detect `verification_mode == "dual"` and override scorer selection:

```python
vm = (_task_meta.get("verification") or {}).get("verification_mode")
if vm == "dual":
    reward_type = "dual"
```

This would then call `get_scorer("dual")` → `DualScorer()`.

**Gotcha:** The `reward_type` auto-detection currently only runs `if reward_type == "binary"` (line 202). If a caller passes `reward_type="artifact"` explicitly but the task has `verification_mode="dual"`, dual scoring is skipped. The PRD says dual mode should run "regardless of experiment-level `reward_type`" — so the dual override must run unconditionally, not nested in the binary-only block. Minor refactor needed.

**Files:** `core/executor.py`  
**Estimated LOC:** ~15

---

### R6 — Graceful degradation when one verifier's input is missing

**Implementable: Yes, already mostly handled.**

`BinaryScorer.score()` returns `ScoreResult(score=0.0, error="tests/test.sh not found")` when test.sh is absent. `ArtifactScorer.score()` returns `ScoreResult(score=0.0, error="answer.json not found")` when answer.json is absent. `DualScorer` just needs to not short-circuit on first failure — run both, combine.

No new files needed beyond DualScorer implementation.

**Files:** `core/scoring.py` (DualScorer already)  
**Estimated LOC:** included in R1

---

### R7 — `codeprobe validate` checks dual task layout

**Already implemented. Zero work needed.**

`validate_cmd.py` lines 249–254:
```python
if vm in ("test_script", "dual") or vm is None:
    results.append(_check_test_script(task_dir))
if vm in ("artifact_eval", "dual"):
    results.append(_check_ground_truth(task_dir))
```
Both checks run for `"dual"` mode. `_check_ground_truth()` validates `answer_type` field presence. This exactly matches the acceptance criteria.

---

### R8 — `dual_composite()` in `analysis/stats.py`

**Implementable: Yes, straightforward.**

`stats.py` has no dual-specific code. A standalone function:
```python
def dual_composite(task: CompletedTask, strategy: str = "min") -> float:
    sd = task.scoring_details
    direct = sd.get("score_direct", task.automated_score)
    artifact = sd.get("score_artifact", 0.0)
    ...
```
Works fine as a pure function on dict access. No imports needed.

**Gotcha:** If `scoring_details` is later typed as `DualScoringDetails` (R10), this function's dict `.get()` calls would need updating. **R8 and R10 are coupled — implement in the right order.**

**Files:** `analysis/stats.py` (or new `analysis/dual.py`)  
**Estimated LOC:** ~30

---

### R9-PM — Per-run scoring isolation

**Implementable: Yes, and urgently needed. The bug is real.**

The race condition is real and documented in the premortem. Evidence in executor.py:

- Lines 337–354: executor copies `answer.json` from workspace into `task_dir` (shared, mutable)
- Lines 209–212: stale-file cleanup removes `answer.txt` and `reward.txt` but **not `answer.json`**
- `ArtifactScorer.score()` reads `task_dir/answer.json` directly (scoring.py:462–465) — no sandbox

Fix: After copying answer files into task_dir, create a per-run temp snapshot dir, copy task_dir contents into it (including answer.json), pass that temp dir to both scorers, delete when done.

**Files:** `core/executor.py`  
**Estimated LOC:** ~35  
**Gotcha:** `BinaryScorer._run_in_sandbox()` already does `shutil.copytree(task_dir, sandbox_task)` — adding a per-run scoring dir causes a double-copy for the direct leg. This is correct but adds latency (~2× disk I/O for the task dir). Acceptable.

---

### R10-PM — Type `scoring_details` as `DualScoringDetails` frozen dataclass

**Implementable: Yes, but higher risk than the PRD implies.**

`CompletedTask.scoring_details` is `dict` (experiment.py:54). Changing this to a typed dataclass breaks:

1. `_restore_checkpointed()` in executor.py line 565: `scoring_details=entry.get("scoring_details", {})` — returns dict from JSON
2. Every existing `CompletedTask` construction with `scoring_details={...}`
3. Checkpoint serialization (dict → JSON → dict round-trip, not dataclass → JSON → dataclass)

**Realistic implementation:** Keep `scoring_details: dict` as the field type, but add a `DualScoringDetails` dataclass as a helper with a `.from_dict(d)` constructor and `.to_dict()` method. Analysis code uses typed access through the helper. This satisfies the PRD's intent (typed access, static analysis catches missing fields) without breaking serialization.

**Files:** `models/experiment.py` (new DualScoringDetails class), optionally `analysis/dual.py`  
**Estimated LOC:** ~40  
**Gotcha:** If implementation naively changes `scoring_details` field type, checkpoint resumption silently produces wrong types for all previously-run experiments. Must preserve dict type on the wire.

---

### R11-PM — E2E integration test through full pipeline

**Implementable: Yes, but significant effort.**

Needs: fixture dual task dir, execute_task → checkpoint save → checkpoint load → summarize_config → report. Each exists but wiring them into one test that asserts artifact scores appear in all 4 output formats (text, HTML, CSV, JSON) is non-trivial.

**Gotcha:** Tests need a live repo fixture to run the agent against. If integration tests mock the adapter (likely), the mock must produce both stdout (for test.sh) and write answer.json to the workspace. This requires understanding the fixture setup pattern used in existing tests.

**Files:** `tests/` (new E2E test file)  
**Estimated LOC:** ~150

---

### R12-PM — Ship 20+ pre-curated dual-verified tasks

**Implementable in principle, but high effort, zero code.**

This is content creation, not engineering. 20 task directories × 3 files each (test.sh, ground_truth.json, instruction.md) = 60 files. For SDLC tasks, test.sh must compile and run against a real repo — which means the task fixtures need an accessible repo or a mocked one. For org-scale tasks, the oracle infrastructure already exists (writer.py `_write_oracle_task` pattern).

**Gotcha:** SDLC dual tasks need test.sh that both verifies code AND grounds truth for file-list comparison. But the PRD deferred SDLC auto-oracle generation to Phase 2. This means Phase 1 SDLC dual tasks need hand-crafted ground_truth.json — someone must manually determine which files an agent should modify. This is labor-intensive and hard to make non-trivial.

**Risk:** May ship with all 20 being org-scale tasks (easier, already have oracle infrastructure) rather than 10+10 split, violating the acceptance criterion.

---

### R13-PM — Phase 2 bead with timeline

**Trivially implementable.** Run `bd update` with a new bead in the `codeprobe-ssf` epic. Not an engineering task.

---

## Cross-Cutting Gotchas Not Captured Per-Requirement

1. **Loaders skip `verification_mode`**: `_build_task()` in loaders/__init__.py does not populate `TaskVerification.verification_mode` from TOML. The field exists on the model but is never loaded. The executor works around this by reading raw JSON directly. Any code that loads a `Task` object and checks `task.verification.verification_mode` will always see the default `"test_script"`. This gap needs closing (add to loaders/_build_task) or the dual detection in the executor stays purely on raw JSON — which is fine for the executor but inconsistent.

2. **`VALID_REWARD_TYPES` bootstrap circularity**: `scoring.py:619` computes `VALID_REWARD_TYPES = frozenset(available_scorers())` at import time. Adding `"dual"` to registry first, then importing scoring.py, works — but the module-level computation means if registry.py is imported before scoring.py fully initializes, there could be subtle ordering issues. Low risk but worth a sanity check.

3. **DualScorer sub-scorer selection is hardcoded**: The PRD says DualScorer "composes a direct scorer (BinaryScorer or ContinuousScorer)". Which one? The task's `reward_type` field? If a task has `reward_type: "continuous"` and `verification_mode: "dual"`, the direct leg should use `ContinuousScorer`. DualScorer needs logic to pick the right direct scorer, not just always use `BinaryScorer`. This adds complexity to construction.

4. **`answer.json` vs `answer.txt`**: Executor handles both (lines 324–354). Oracle tasks use `answer.txt`; comprehension/artifact tasks use `answer.json`. Dual tasks need `answer.json`. Stale-file cleanup (lines 209–212) removes `answer.txt` and `reward.txt` but not `answer.json` — confirmed bug for R9-PM. The fix must add `"answer.json"` to the stale-file list.

---

## Final Verdict: **Needs Rework (Not Blocked)**

The PRD is well-researched and the architecture is sound, but several Phase 1 requirements have implementation gaps that need resolution before coding starts:

| Issue | Severity | Requirements Affected |
|---|---|---|
| `ScoreResult` has no `scoring_details` field → sub-scores have no channel to `CompletedTask` | High | R1, R6, R10 |
| `DualScorer` can't receive `scoring_policy` via protocol without reading disk or changing construction | Medium | R3, R5 |
| Loader never populates `verification_mode` into `Task` model | Medium | R3, R5 |
| `CompletedTask.scoring_details` is `dict` → R10 "typed dataclass" is a breaking change without a migration plan | Medium | R10, R8 |
| `answer.json` missing from stale-file cleanup | High | R9-PM |
| R7 is already done — no work needed | Good news | R7 |

**Recommended action before implementation:** Add `scoring_details: dict = field(default_factory=dict)` to `ScoreResult`. Decide whether DualScorer reads policy from disk or is constructed with it by the executor. Define `DualScoringDetails` as a helper (not a field type change on `CompletedTask`). Then R1–R10 can be implemented cleanly without collateral breakage.


Changes   +0 -0
Requests  1 Premium (3m 29s)
Tokens    ↑ 486.3k • ↓ 10.2k • 416.4k (cached)
