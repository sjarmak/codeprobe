**Design Strengths**

1. The scorer architecture is already close to a compositor pattern. `Scorer` is a small protocol returning `ScoreResult`, and the direct/artifact scorers have compatible signatures, so a `DualScorer` can compose them without disturbing most callers: [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):59, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):67.

2. The existing failure behavior mostly supports graceful degradation. `BinaryScorer` returns a failed `ScoreResult` when `tests/test.sh` is missing instead of raising, and `ArtifactScorer` does the same for missing/invalid `ground_truth.json` or `answer.json`: [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):217, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):219, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):475, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):482, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):499.

3. The taxonomy already reserves `"dual"` as a valid verification mode. That reduces schema churn for Phase 1 because validators and models do not need a new enum concept: [task.py](/home/ds/projects/codeprobe/src/codeprobe/models/task.py):66.

4. `codeprobe validate` already partially understands dual layout. It checks `tests/test.sh` for `test_script`/`dual` and `tests/ground_truth.json` for `artifact_eval`/`dual`: [validate_cmd.py](/home/ds/projects/codeprobe/src/codeprobe/cli/validate_cmd.py):248.

5. The checkpoint and artifact persistence path can already carry arbitrary scorer metadata. `CompletedTask.scoring_details` is serialized through checkpoint restore and saved into per-run `scoring.json`, which gives dual sub-scores a natural propagation path once typed or normalized: [experiment.py](/home/ds/projects/codeprobe/src/codeprobe/models/experiment.py):54, [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):551, [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):524.

**Design Weaknesses**

1. The PRD’s central feature is not currently implementable through the scorer API as written. `ScoreResult` only has `score`, `passed`, and `error`; it has no `scoring_details` or typed details payload, so `DualScorer.score()` cannot return the required `score_direct`, `score_artifact`, `passed_direct`, and `passed_artifact` without changing the result contract or hiding structured data in `error`: [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):45.

2. `"dual"` is valid as task metadata but not valid as a scorer. The scorer registry contains `artifact`, `binary`, `continuous`, `checkpoint`, and `test_ratio`, but no `dual`; therefore `get_scorer("dual")` and `resolve_scorer("dual")` will fail: [registry.py](/home/ds/projects/codeprobe/src/codeprobe/core/registry.py):98, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):622.

3. Executor auto-detection is keyed to `verification.reward_type`, not `verification.verification_mode`. A task with `verification_mode: "dual"` but no explicit `reward_type: "dual"` will still run the experiment-level/default binary scorer: [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):199. This directly misses R5.

4. The executor still mutates the shared `task_dir` during scoring. It deletes stale `answer.txt` and `reward.txt` but not `answer.json`, then copies workspace `answer.txt` and `answer.json` into the task directory before scoring: [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):207, [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):337, [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):352. This confirms the premortem risk: parallel repeats can race on `answer.json`, and sequential runs can leak old `answer.json`.

5. The proposed per-run scoring directory is underspecified for direct code verification. `BinaryScorer` copies `task_dir` into a sandbox, but mined `tests/test.sh` scripts `cd` to the absolute `repo_path`, so tests still exercise the mutable repo/worktree, not only the scoring directory: [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):154, [mining/writer.py](/home/ds/projects/codeprobe/src/codeprobe/mining/writer.py):380. Per-run scoring isolation must include a run-specific repo/worktree/artifact binding, not just a copied task directory.

6. `scoring_policy` has no data-model or loader support. `TaskVerification` has `reward_type`, oracle fields, and checkpoints, but no `scoring_policy`, `weight_direct`, or `weight_artifact`: [task.py](/home/ds/projects/codeprobe/src/codeprobe/models/task.py):119. The loader only reads `type`, `command`, `reward_type`, and checkpoints into `TaskVerification`: [loaders/__init__.py](/home/ds/projects/codeprobe/src/codeprobe/loaders/__init__.py):96. R3 needs model, loader, metadata, executor, and scorer changes.

7. The PRD’s oracle JSON shape conflicts with current `ArtifactScorer`. The PRD says Phase 2 should generate `{"answer_type": "file_list", "expected": [...]}`, but `_score_new_format()` requires `"answer"` whenever `"answer_type"` exists; `"expected"` is only accepted in the legacy format when `answer_type` is absent: [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):515, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):521, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):554. That would make PRD-generated oracles fail validation/scoring unless the format is reconciled.

8. The proposed registry design is too context-free. `resolve_scorer()` instantiates scorer classes with no arguments: [registry.py](/home/ds/projects/codeprobe/src/codeprobe/core/registry.py):24. But `DualScorer` needs to know whether the direct leg is binary, continuous, checkpoint, or something else, plus scoring policy. That metadata is task-specific, so a plain `get_scorer("dual")` factory is probably insufficient unless `DualScorer` reads configuration from files in `task_dir` or `get_scorer` accepts task metadata.

**Missing Considerations**

1. Direct-leg selection needs a precise rule. R1 says direct scorer is `BinaryScorer or ContinuousScorer`, but R5 says dual ignores experiment-level `reward_type`. The design needs an explicit per-task field such as `direct_reward_type`, or a rule that `reward_type="dual"` plus `direct_reward_type="binary|continuous|checkpoint"` is required.

2. `passed` semantics for artifact scores are currently weak. `ArtifactScorer` treats any F1 greater than zero as passed for file lists: [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):538. Dual reporting like `artifact:PASS` will be misleading unless there is a threshold or policy-specific pass criterion.

3. `ScoreResult` versus `CompletedTask.scoring_details` needs a clean ownership boundary. Today executor discards anything except `score_result.passed` and `score_result.error`: [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):395. If sub-score details live only on `CompletedTask`, executor must know about dual. If they live on `ScoreResult`, all serialization/reporting can stay scorer-driven.

4. The PRD calls for `DualScoringDetails` as a frozen dataclass, but `CompletedTask.scoring_details` is a raw `dict`: [experiment.py](/home/ds/projects/codeprobe/src/codeprobe/models/experiment.py):54. Freezing a nested `extra: dict` is also only shallowly frozen, so the design should either use an immutable mapping or accept that this is typed but not truly immutable.

5. Validation checks existence and `answer_type`, but not full scorer compatibility. It does not require an `"answer"` field, validate answer type values, validate weighted policy fields, or check that `test.sh` and `ground_truth.json` are mutually consistent for dual: [validate_cmd.py](/home/ds/projects/codeprobe/src/codeprobe/cli/validate_cmd.py):160.

6. Writer support is split. The SDLC writer only emits `instruction.md`, `tests/test.sh`, and `metadata.json`; no `ground_truth.json` branch exists for `verification_mode="dual"`: [mining/writer.py](/home/ds/projects/codeprobe/src/codeprobe/mining/writer.py):270. The comprehension writer emits `tests/ground_truth.json` and `answer.json` instructions but no `tests/test.sh`: [comprehension_writer.py](/home/ds/projects/codeprobe/src/codeprobe/mining/comprehension_writer.py):26. R4 needs either a shared writer path or explicit dual branches in both families.

7. Reporting/event output will silently collapse dual scores unless the event and CLI listener payloads grow. `TaskScored` currently receives only `automated_score`, not `scoring_details`: [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):720. R15 therefore needs event model and listener changes, not just formatting.

**Verdict**

The design direction holds up: composing the existing direct and artifact scorers is the right architectural move, and defaulting `automated_score` to the direct leg is the safest compatibility choice for mixed task sets.

But the current PRD is not yet implementation-ready. The biggest gaps are scorer result typing, task-specific scorer configuration, executor isolation, and oracle schema consistency. In particular, “copy task files + answer.json into a temp scoring directory” is not enough because existing test scripts jump back to an absolute repo path. I would approve Phase 1 only after the design adds a concrete execution contract for dual runs: how the direct scorer is selected, where artifacts are staged, how repo/worktree isolation is guaranteed, and how typed sub-scores flow through checkpoints, reports, events, and CLI output.