**Findings**

1. **Critical: `verification_mode="dual"` is currently a no-op at runtime.**  
   Failure scenario: a task has `metadata.json` with `verification.verification_mode = "dual"` and experiment `reward_type` remains default `"binary"`. `execute_task()` only auto-detects `verification.reward_type`, not `verification_mode`, so it keeps `reward_type="binary"` and runs only `BinaryScorer`. The artifact leg never runs.  
   Code: [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):199, [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):202, [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):375

2. **Critical: `dual` is in the task enum but not in the scorer registry, so explicit dual scoring fails.**  
   Failure scenario: a user tries to force `reward_type="dual"` or a loader eventually maps `verification_mode="dual"` to `reward_type="dual"`. `get_scorer("dual")` raises `ValueError` because `_SCORER_BUILTINS` lacks `"dual"`. This violates R2 and blocks the whole design.  
   Code: [task.py](/home/ds/projects/codeprobe/src/codeprobe/models/task.py):125, [registry.py](/home/ds/projects/codeprobe/src/codeprobe/core/registry.py):98, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):622

3. **Critical: the PRD’s proposed ground-truth format contradicts `ArtifactScorer`.**  
   Failure scenario: Phase 2 generates `{"answer_type": "file_list", "expected": [...]}` as specified by R16. `ArtifactScorer` sees `answer_type`, enters new-format scoring, and requires `ground_truth.json["answer"]`; it returns `0.0` with `"ground_truth.json missing 'answer' field"`. Every generated dual oracle would fail artifact scoring.  
   Code: [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):515, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):521, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):524, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):554

4. **Critical: per-run scoring isolation cannot work with generated test scripts as written.**  
   Failure scenario: R9 says executor should copy task files plus `answer.json` into a temp scoring directory and pass that to `DualScorer`. But mined `tests/test.sh` hardcodes `cd {repo_path}` into the original repo path. Copying task files does not isolate the direct verifier; the test script escapes the scoring directory and reads/mutates the real repo/workspace. Parallel repeats can still cross-contaminate.  
   Code: [writer.py](/home/ds/projects/codeprobe/src/codeprobe/mining/writer.py):375, [writer.py](/home/ds/projects/codeprobe/src/codeprobe/mining/writer.py):380, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):117

5. **Critical: executor still mutates shared `task_dir` with agent artifacts.**  
   Failure scenario: two repeats of the same dual task run in parallel. Both copy their workspace `answer.json` to the same `task_dir / "answer.json"` before scoring. One artifact scorer can read the other run’s answer. Sequential runs can also inherit a stale `answer.json` because stale cleanup only removes `answer.txt` and `reward.txt`. This is exactly the premortem risk, still present.  
   Code: [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):207, [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):209, [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):350, [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):352

6. **Critical: `ScoreResult` cannot carry the PRD-required dual sub-score payload.**  
   Failure scenario: `DualScorer.score()` is required to return `ScoreResult` with `scoring_details` containing `score_direct`, `score_artifact`, `passed_direct`, and `passed_artifact`. `ScoreResult` only has `score`, `passed`, and `error`. Even if `DualScorer` computes both legs, the typed result object has nowhere to put them.  
   Code: [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):45, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):49

7. **Critical: executor would discard dual details even if a scorer produced them.**  
   Failure scenario: a future `DualScorer` returns extra detail somehow. `execute_task()` constructs `CompletedTask.scoring_details` from only `"passed"` and `"error"`. The direct/artifact sub-scores vanish before checkpointing, reports, or artifacts can see them.  
   Code: [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):389, [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):395, [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):400

8. **High: `scoring_policy` is absent from the model, loader, and executor.**  
   Failure scenario: a task has `[verification] scoring_policy = "min"` with weights. The loader drops it, `TaskVerification` has no fields for it, and `execute_task()` never reads it. The automated score remains whatever single scorer returns, violating R3.  
   Code: [task.py](/home/ds/projects/codeprobe/src/codeprobe/models/task.py):119, [task.py](/home/ds/projects/codeprobe/src/codeprobe/models/task.py):129, [loaders/__init__.py](/home/ds/projects/codeprobe/src/codeprobe/loaders/__init__.py):96, [loaders/__init__.py](/home/ds/projects/codeprobe/src/codeprobe/loaders/__init__.py):99

9. **High: existing org-scale artifact tasks are not compatible with the PRD’s `ArtifactScorer` path.**  
   Failure scenario: the PRD says org-scale and comprehension tasks are primary Phase 1 consumers of dual verification. Existing org-scale writer emits `answer.txt` instructions and root-level `ground_truth.json` with `oracle_type`/`expected`, while `ArtifactScorer` requires `answer.json` and either new-format `answer` or legacy `expected` as a file list. Dualizing these tasks naively creates one artifact path for comprehension and a different oracle path for org-scale.  
   Code: [writer.py](/home/ds/projects/codeprobe/src/codeprobe/mining/writer.py):546, [writer.py](/home/ds/projects/codeprobe/src/codeprobe/mining/writer.py):580, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):461, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):498

10. **High: `write_task_dir()` does not emit a dual layout.**  
    Failure scenario: a `Task` has `verification_mode="dual"`. The non-oracle writer still emits only `instruction.md`, `tests/test.sh`, and `metadata.json`; no `tests/ground_truth.json`, no answer schema section, and no instruction to produce `answer.json`. R4 and R14 fail for mined SDLC-style dual tasks.  
    Code: [writer.py](/home/ds/projects/codeprobe/src/codeprobe/mining/writer.py):277, [writer.py](/home/ds/projects/codeprobe/src/codeprobe/mining/writer.py):336, [writer.py](/home/ds/projects/codeprobe/src/codeprobe/mining/writer.py):387

11. **High: validation accepts dual metadata but does not prove the task is scoreable.**  
    Failure scenario: a dual task contains `tests/ground_truth.json` with only `{"answer_type": "file_list"}`. `codeprobe validate` passes the ground-truth schema check because it only requires `answer_type`, but `ArtifactScorer` will fail because `answer` is missing. R7’s “schema validity” acceptance is too weak to catch real failures.  
    Code: [validate_cmd.py](/home/ds/projects/codeprobe/src/codeprobe/cli/validate_cmd.py):160, [validate_cmd.py](/home/ds/projects/codeprobe/src/codeprobe/cli/validate_cmd.py):253, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):524

12. **High: `DualScoringDetails` cannot be enforced through persistence because `CompletedTask.scoring_details` is an untyped dict.**  
    Failure scenario: one writer stores `"artifact_score"` while analysis expects `"score_artifact"`. Static analysis will not catch it, checkpoint load will preserve the wrong dict, and reports silently omit it. This directly defeats R10-PM.  
    Code: [experiment.py](/home/ds/projects/codeprobe/src/codeprobe/models/experiment.py):37, [experiment.py](/home/ds/projects/codeprobe/src/codeprobe/models/experiment.py):54, [executor.py](/home/ds/projects/codeprobe/src/codeprobe/core/executor.py):565, [core/experiment.py](/home/ds/projects/codeprobe/src/codeprobe/core/experiment.py):228

13. **High: reports and stats cannot satisfy the E2E acceptance test because they never read artifact scores.**  
    Failure scenario: a checkpoint contains valid `scoring_details.score_artifact = 0.4`. `summarize_config()` computes only from `automated_score`; text/HTML/CSV/JSON task rows include only the aggregate score. The artifact dimension is dropped from all formats, exactly the premortem risk.  
    Code: [stats.py](/home/ds/projects/codeprobe/src/codeprobe/analysis/stats.py):212, [report.py](/home/ds/projects/codeprobe/src/codeprobe/analysis/report.py):174, [report.py](/home/ds/projects/codeprobe/src/codeprobe/analysis/report.py):217, [report.py](/home/ds/projects/codeprobe/src/codeprobe/analysis/report.py):427

14. **Medium: CLI sub-score output is structurally impossible with the current event model.**  
    Failure scenario: R15 wants `task-id: 0.70 (code:PASS artifact:0.40)`. `TaskScored` contains only `automated_score`; plain and rich listeners format only that single score. Without extending the event payload, dual details cannot reach live CLI output.  
    Code: [events.py](/home/ds/projects/codeprobe/src/codeprobe/core/events.py):45, [run_cmd.py](/home/ds/projects/codeprobe/src/codeprobe/cli/run_cmd.py):73, [rich_display.py](/home/ds/projects/codeprobe/src/codeprobe/cli/rich_display.py):131

15. **Medium: pass semantics are inconsistent and will misclassify partial dual artifacts.**  
    Failure scenario: artifact F1 of `0.01` marks `passed=True` in `ArtifactScorer`, while analysis pass rate uses `PASS_THRESHOLD = 0.5`. A dual task can report `passed_artifact=True` in details but fail in summary/report pass logic. This undermines any 2x2 matrix or gate policy unless pass thresholds are centralized.  
    Code: [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):538, [scoring.py](/home/ds/projects/codeprobe/src/codeprobe/core/scoring.py):540, [stats.py](/home/ds/projects/codeprobe/src/codeprobe/analysis/stats.py):16, [stats.py](/home/ds/projects/codeprobe/src/codeprobe/analysis/stats.py):213

No code changes were made; this was a read-only adversarial review.