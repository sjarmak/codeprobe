● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ 1 line read

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ 1 line read

● Read report.py
  │ src/codeprobe/analysis/report.py
  └ 1 line read

● Read prd_dual_verifier_mining.md
  └ 199 lines read

● Read experiment.py
  │ src/codeprobe/models/experiment.py
  └ 74 lines read

● Read registry.py
  │ src/codeprobe/core/registry.py
  └ 114 lines read

● Read task.py
  │ src/codeprobe/models/task.py
  └ 150 lines read

● Read stats.py
  │ src/codeprobe/analysis/stats.py
  └ 455 lines read

Let me read the large files in sections:

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L1:120 (120 lines read)

● Read report.py
  │ src/codeprobe/analysis/report.py
  └ L1:100 (99 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L1:100 (100 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L120:350 (231 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L100:300 (200 lines read)

● Read report.py
  │ src/codeprobe/analysis/report.py
  └ L100:350 (251 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L300:450 (151 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L350:550 (201 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L550:700 (126 lines read)

Here is the architecture review.

---

## Architecture Review: Dual Verifier Mining PRD

---

### 1. Design Strengths

**Compositor pattern is sound.** Composing `BinaryScorer` + `ArtifactScorer` into `DualScorer` avoids duplicating the `_run_in_sandbox` machinery (`scoring.py:133-189`). Each leg already has graceful degradation (missing `test.sh` → `ScoreResult(score=0.0, error=…)` at line 219-221; missing `answer.json` → same at lines 499-511).

**`scoring_details` extension point is already wired.** `CompletedTask.scoring_details: dict` (`experiment.py:54`) gives DualScorer a place to store sub-scores without touching the public `automated_score` field. The executor already writes into it at `executor.py:400-402`.

**Aggregation decision is architecturally correct.** `automated_score = score_direct` default (PRD §Score Aggregation) avoids mixed-set distortion when running 50 `test_script` tasks alongside 5 `dual` tasks. Deferring composite to a view function at analysis time is reversible; baking it into `automated_score` at write time is not.

**Registry extension mechanism is minimal.** Adding `"dual"` to `_SCORER_BUILTINS` (`registry.py:98-104`) and registering via `codeprobe.scorers` entry-point group is a one-line change that respects the existing plugin pattern.

**`VERIFICATION_MODES` enum already includes `"dual"`.** `task.py:67-73` — the taxonomy is correct; only the implementation is missing.

---

### 2. Design Weaknesses

**W1 (Critical): `"dual"` absent from scorer registry.** `registry.py:98-104` has five scorers; `"dual"` is not one of them. `VALID_REWARD_TYPES` in `scoring.py:619` is computed from `available_scorers()` at module import — it will not include `"dual"` until the registry entry exists. Consequence: a task with `verification_mode: "dual"` in metadata.json hits executor.py's auto-detect at lines 202-205, finds no matching `reward_type`, and silently falls back to `"binary"`. The artifact leg never runs. This is a silent correctness failure in production.

**W2 (Critical): Generic `_resolve()` factory is incompatible with `DualScorer`.** `registry.py:32` calls `cls()` with no arguments. `DualScorer.__init__` will require two sub-scorer arguments (direct + artifact scorers). Pushing `DualScorer` through the generic registry path will either raise `TypeError` on construction or require `DualScorer` to hard-code its sub-scorers internally, breaking the compositor's configurability. Either `get_scorer()` special-cases `"dual"`, or `_resolve()` needs a factory-function escape hatch.

**W3 (Critical): `ScoreResult` has no mechanism to carry sub-scores.** `scoring.py:44-51` — `ScoreResult` only has `score`, `passed`, `error`. The sub-scores from both legs must reach `CompletedTask.scoring_details`, but the `Scorer` Protocol returns only `ScoreResult`. The path through `executor.py:389` (`scorer.score(...)`) into `scoring_details` at line 400 currently writes only `{"passed": …, "error": …}`. There is no mechanism for `DualScorer.score()` to return `score_direct`, `score_artifact`, `passed_direct`, `passed_artifact` — unless `ScoreResult` gains an optional `details: dict` field, or `executor.py` special-cases `DualScorer` (which would add coupling). The PRD mandates `DualScoringDetails` (R10-PM) but doesn't resolve this data-flow gap.

**W4 (High): `ArtifactScorer` reads from mutable `task_dir`, not a sandbox.** `scoring.py:461-466` (`_find_answer_file`) reads `answer.json` from the real `task_dir`. `BinaryScorer` runs in a sandbox copy (`scoring.py:154`). In dual mode: test.sh executes in a clean snapshot; ArtifactScorer reads from the shared mutable directory. The executor copies `answer.json` into `task_dir` at lines 350-354 before scoring — but this is the same directory other parallel runs may be writing into. The per-run scoring directory mitigation (R9-PM) is described but not implemented.

**W5 (High): `answer.json` not in stale-file cleanup.** `executor.py:209-212` cleans `answer.txt` and `reward.txt` but not `answer.json`. A prior run's `answer.json` left in `task_dir` leaks into the next task's `ArtifactScorer`. The PRD explicitly flags this ("answer.json added to stale-file cleanup list") but it remains unfixed.

---

### 3. Missing Considerations

**M1: `scoring_policy` and weight fields absent from `TaskVerification`.** `task.py:119-136` has `reward_type`, `oracle_type`, `oracle_tiers`, etc., but no `scoring_policy`, `weight_direct`, or `weight_artifact`. R3 requires these to flow from `task.toml` → `TaskVerification` → executor → `DualScorer`. Without the model fields, the task.toml fields are silently dropped at parse time.

**M2: `dual_composite()` missing from `stats.py`; `scoring_details` invisible to all report formats.** `stats.py` summarizes only `automated_score` (lines 212, 295). `report.py`'s `_build_task_rows()` (lines 203-230) and `_CSV_COLUMNS` (lines 256-271) have no `score_direct`, `score_artifact` columns. `format_text_report()` (lines 174-184) emits no sub-scores. R8 requires `dual_composite()` in stats.py; R11-PM requires an E2E test that asserts artifact scores appear in all output formats. Neither exists. Artifact sub-scores will flow through serialization to checkpoint files but vanish from every report rendered to the user — exactly premortem risk #2.

**M3: Instruction.md for dual tasks (R14).** Nothing in the writer or task format currently instructs agents to produce both code changes AND `answer.json`. A dual task launched today would give agents no signal that an artifact is expected. Without this, the artifact leg will universally score 0.0.

**M4: `get_scorer()` return type annotation will be stale.** `scoring.py:622-624` — the union type must be extended to include `DualScorer`. Minor, but breaks static analysis until updated.

**M5: Repeats × dual variance decomposition.** `stats.py:summarize_config()` computes a single `mean_score` and `pass_rate` from `automated_score`. If test.sh is deterministic but `answer.json` varies across repeats, the variance signal is collapsed. The PRD's Open Question 2 defers this but doesn't put it in the technical debt register or assign it to a Phase 2 bead — it may be forgotten.

**M6: Over-modification gap in `file_list` oracle.** `ArtifactScorer._compute_f1()` (`scoring.py:573-587`) penalizes via precision (extra files lower precision). An agent that correctly fixes a bug by refactoring 4 files instead of the expected 3 gets F1 < 1.0 and could fail the artifact leg despite a passing test.sh. The PRD's Open Question 4 acknowledges this but leaves it unresolved. The interaction with `min()` composite scoring is especially punishing — a valid-but-different solution becomes a dual failure.

**M7: `codeprobe validate` not dual-aware.** R7 requires `validate` to check both `tests/test.sh` and `ground_truth.json` for dual tasks. No evidence this exists in the current codebase.

**M8: Two sandbox copies per dual run.** When `DualScorer` composes `BinaryScorer` + `ArtifactScorer`, `BinaryScorer` will call `_run_in_sandbox()` (which does `shutil.copytree` of the full task dir). If the per-run scoring directory (R9-PM) is added as the canonical snapshot, `ArtifactScorer` should read from that snapshot — not the original `task_dir`. The design needs to ensure DualScorer creates **one** snapshot and passes it to both sub-scorers, not two sequential copies.

---

### 4. Verdict

**The PRD is architecturally sound; the foundation is adequate; three implementation gaps must be resolved before writing code.**

The problem statement, phased approach, and aggregation decision are all correct. The premortem surfaced the real risks. The existing codebase (`Scorer` Protocol, `_run_in_sandbox`, `ArtifactScorer`, `scoring_details` on `CompletedTask`) was clearly designed with extension in mind and provides a legitimate foundation.

**Three blockers to resolve before implementation starts:**

1. **Registry constructor mismatch (W2).** Decide whether `get_scorer("dual")` special-cases construction or `DualScorer.__init__` takes no args and hard-codes sub-scorers. The latter limits testability; the former breaks the clean generic pattern. A factory function registered in `_SCORER_BUILTINS` (mapping name → callable that returns an instance) is the cleanest fix.

2. **Sub-score data flow (W3).** `ScoreResult` needs an optional `details: dict` field, or `executor.py` must call a DualScorer-specific method alongside `.score()`. Extend `ScoreResult` — it's the minimum-coupling path and `DualScoringDetails` (R10-PM) becomes the contents of `details`.

3. **`scoring_policy` model fields (M1).** Add `scoring_policy: str = ""`, `weight_direct: float = 0.5`, `weight_artifact: float = 0.5` to `TaskVerification` before any task.toml serialization code is written, to avoid a schema migration.

Fix W5 (answer.json stale-file cleanup) as a one-liner at `executor.py:209` — it's pre-existing and can be done now without waiting for DualScorer. Everything else (dual_composite, validate, report format changes, instruction.md) maps cleanly to existing extension points once the three blockers are resolved.


Changes   +0 -0
Requests  1 Premium (2m 19s)
Tokens    ↑ 300.0k • ↓ 6.8k • 207.9k (cached)
