# Adversarial Code Review — Dual-Verifier-Mining Implementation

**Commit range:** 5cc6daf..HEAD (9 prd-build commits + fix)
**Reviewer:** codex (gpt-5.4)
**Scope:** Implementation review of landed code, not PRD review.

## Findings

1. **CRITICAL: Direct verifier still runs against the original repo, not the per-run worktree**
   Failure scenario: two dual runs acquire distinct worktrees, agents write changes there, but the mined `tests/test.sh` still executes `cd <original repo_path>`, so direct verification checks shared/original state and parallel runs can trample or miss the agent changes entirely.
   Code: src/codeprobe/mining/writer.py:529, src/codeprobe/mining/writer.py:530, src/codeprobe/core/executor.py:249, src/codeprobe/core/executor.py:472
   Fix: generate `test.sh` to use a runtime `TASK_REPO_ROOT`/`CODEPROBE_WORKTREE` env var, and set that env var to `_effective_wt` when scoring; reject hardcoded repo paths in dual tasks.

2. **CRITICAL: Weighted scoring accepts negative and over-1 weights, allowing artifact failure to pass**
   Failure scenario: metadata uses `scoring_policy="weighted"`, `weight_direct=2`, `weight_artifact=-1`; direct score 1 and artifact score 0 produces composite 2, clamped to 1, so a failed artifact leg passes.
   Code: src/codeprobe/core/scoring.py:698, src/codeprobe/core/scoring.py:733, src/codeprobe/core/scoring.py:741, src/codeprobe/cli/validate_cmd.py:304, src/codeprobe/cli/validate_cmd.py:308
   Fix: validate `math.isfinite(weight)` and `0.0 <= weight <= 1.0` for both weights in loader, validator, and `DualScorer`; fail closed, do not clamp away invalid policy effects.

3. **HIGH: Missing or unreadable dual metadata silently downgrades to direct-only scoring**
   Failure scenario: a dual task has malformed `metadata.json` in the scoring snapshot; `DualScorer` falls back to `{}`, uses binary/direct reward and empty policy, then ignores the artifact score for the composite. A direct pass with missing or invalid `answer.json` still passes.
   Code: src/codeprobe/core/scoring.py:671, src/codeprobe/core/scoring.py:681, src/codeprobe/core/scoring.py:695, src/codeprobe/core/scoring.py:738
   Fix: for `DualScorer`, require readable metadata with `verification_mode="dual"` and an explicit policy, or default dual policy to `min`; metadata parse failure should return score 0 with an error.

4. **HIGH: Invalid weighted values are hidden by defaults in both validation and scoring**
   Failure scenario: `weight_direct="abc"` and `weight_artifact="abc"` validate as 0.5 + 0.5, and the scorer also uses 0.5 defaults, so malformed task metadata becomes a valid weighted run.
   Code: src/codeprobe/cli/validate_cmd.py:252, src/codeprobe/cli/validate_cmd.py:263, src/codeprobe/cli/validate_cmd.py:304, src/codeprobe/core/scoring.py:698, src/codeprobe/core/scoring.py:703
   Fix: return a failed check when a present weight cannot be parsed; in `DualScorer`, treat invalid configured weights as scorer errors, not defaults.

5. **HIGH: Stale `answer.json` from the original repo can be copied into an isolated dual run**
   Failure scenario: owned dual isolation is active, the agent writes no `answer.json` in its worktree, but a stale `repo_path/answer.json` exists from a prior/manual run; executor copies that fallback into the scoring sandbox and the artifact leg can pass with another run's answer.
   Code: src/codeprobe/core/executor.py:393, src/codeprobe/core/executor.py:395, src/codeprobe/core/executor.py:397, src/codeprobe/core/executor.py:466
   Fix: in dual mode with `_effective_wt`, never fall back to `repo_path/answer.json`; remove repo-root stale artifacts before the run or require the artifact to come from the effective worktree only.

6. **HIGH: Artifact-copy failures are swallowed, then composite scoring can still pass**
   Failure scenario: the agent writes `answer.json`, but `copy2` fails due permissions, disappearing file, or partial artifact; executor discards the `OSError`, `ArtifactScorer` reports missing answer, and direct-only/default or bad weighted policy can still mark the task completed.
   Code: src/codeprobe/core/executor.py:466, src/codeprobe/core/executor.py:469, src/codeprobe/core/executor.py:470, src/codeprobe/core/scoring.py:738
   Fix: in dual mode, return an error result when the expected artifact cannot be copied, and make missing artifact fatal for pass status under every dual policy.

7. **HIGH: The shared task directory is still mutated before the per-run scoring sandbox**
   Failure scenario: concurrent runs of the same task share `task_dir`; each run deletes `task_dir/answer.json`, `answer.txt`, and `reward.txt` before snapshotting, so the implementation is not read-only with respect to task fixtures and can race with another snapshot or destroy a legitimate fixture artifact.
   Code: src/codeprobe/core/executor.py:224, src/codeprobe/core/executor.py:226, src/codeprobe/core/executor.py:229, tests/test_executor_dual_isolation.py:285, tests/test_executor_dual_isolation.py:311
   Fix: do not delete from `task_dir`; copy to `scoring_dir` first and remove stale root artifacts only inside that per-run directory.

8. **HIGH: Shell command allowlisting is prefix-based and still allows command injection**
   Failure scenario: a verification command beginning with an allowed prefix but containing shell metacharacters is written directly into `test.sh`; `pytest tests; curl ...` or equivalent payloads pass the prefix test and execute under bash.
   Code: src/codeprobe/mining/writer.py:520, src/codeprobe/mining/writer.py:522, src/codeprobe/mining/writer.py:525, src/codeprobe/mining/writer.py:530
   Fix: parse commands with `shlex.split`, compare argv[0] and fixed subcommands against an allowlist, and write an argv-safe wrapper instead of interpolating raw shell.

9. **MEDIUM: Dual ground-truth validation accepts schemas the scorer interprets incorrectly**
   Failure scenario: `tests/ground_truth.json` has `"answer_type": "file_list"` but `"answer": "src/foo.py"` as a string; validation passes because it only checks for an `answer` key, and `_compute_f1` iterates characters, producing meaningless scores.
   Code: src/codeprobe/cli/validate_cmd.py:215, src/codeprobe/cli/validate_cmd.py:221, src/codeprobe/core/scoring.py:541, src/codeprobe/core/scoring.py:577, src/codeprobe/core/scoring.py:579
   Fix: validate `answer_type` and answer payload type for every dual task; in `ArtifactScorer`, reject non-list values for `file_list` before computing F1.

10. **MEDIUM: `answer_type` in `answer.json` is documented but not enforced**
   Failure scenario: the prompt tells agents `answer_type` must match ground truth, but `ArtifactScorer` ignores `answer_data["answer_type"]`; a mismatched or missing type still scores using only `answer`, weakening artifact semantics and reportability.
   Code: src/codeprobe/mining/writer.py:414, src/codeprobe/mining/writer.py:416, src/codeprobe/core/scoring.py:523, src/codeprobe/core/scoring.py:525, src/codeprobe/core/scoring.py:541
   Fix: require `answer_data.get("answer_type") == gt["answer_type"]` for the new format before dispatching to type-specific scoring.

11. **MEDIUM: Boolean coercion inflates dual pass rates from non-boolean serialized values**
   Failure scenario: imported/checkpointed `scoring_details` contains `"passed_artifact": "False"`; `bool("False")` is `True`, so stats and gate composites report an artifact pass even though the serialized value says false.
   Code: src/codeprobe/models/experiment.py:61, src/codeprobe/analysis/stats.py:162, src/codeprobe/analysis/stats.py:163, src/codeprobe/analysis/dual.py:60, src/codeprobe/analysis/dual.py:65
   Fix: parse booleans strictly (`is True` / `is False`, or explicit string parser) and fall back to score thresholds when the stored type is not a bool.

12. **MEDIUM: Typed dual details lose leg error fields**
   Failure scenario: `DualScorer` emits `error_direct` / `error_artifact` at the top level of `ScoreResult.details`, but `DualScoringDetails.from_dict()` only preserves an existing nested `extra` dict. Any consumer using the typed view loses the reason a leg failed.
   Code: src/codeprobe/core/scoring.py:724, src/codeprobe/core/scoring.py:727, src/codeprobe/models/experiment.py:55, src/codeprobe/models/experiment.py:64, src/codeprobe/models/experiment.py:67
   Fix: construct `extra` from all unknown keys in the source dict, or add explicit error fields to `DualScoringDetails`.

13. **MEDIUM: Reports mark low nonzero dual composites as passing**
   Failure scenario: weighted or mean dual scoring returns 0.25 with `ScoreResult.passed=False`; text/HTML reports display `Y` because they use `automated_score > 0` instead of the shared pass threshold or the stored `passed` flag.
   Code: src/codeprobe/core/scoring.py:741, src/codeprobe/core/scoring.py:742, src/codeprobe/analysis/report.py:197, src/codeprobe/analysis/report.py:514
   Fix: use `task.scoring_details["passed"]` when present, otherwise `automated_score >= PASS_THRESHOLD`, in all report formats.

14. **MEDIUM: The dual writer emits an empty artifact oracle by default**
   Failure scenario: mined dual tasks are written with `tests/ground_truth.json` containing `"answer": []`; `ArtifactScorer` returns 0 whenever the expected set is empty, so generated dual tasks are either unpassable under strict policies or silently pass only because direct-only/default policy ignores the artifact leg.
   Code: src/codeprobe/mining/writer.py:536, src/codeprobe/mining/writer.py:541, src/codeprobe/core/scoring.py:581, src/codeprobe/core/scoring.py:582
   Fix: do not mark tasks as `verification_mode="dual"` until a populated oracle exists, or make writer require a non-empty artifact oracle before emitting dual layout.

15. **MEDIUM: Isolation tests mask the real DualScorer and real artifact I/O race surface**
   Failure scenario: the parallel stress test proves unique temp paths for a fake scorer, but it never runs `BinaryScorer`, `ArtifactScorer`, `test.sh`, `ground_truth.json`, answer discovery, or fallback copying, so the direct-worktree and stale-answer bugs above are not covered.
   Code: tests/test_executor_dual_isolation.py:42, tests/test_executor_dual_isolation.py:49, tests/test_executor_dual_isolation.py:492, tests/test_executor_dual_isolation.py:506, tests/test_dual_e2e.py:227
   Fix: add a parallel test that uses the real `DualScorer`, real generated `tests/test.sh`, distinct per-run answers, and owned worktree isolation rather than a caller-supplied worktree.

## Test Gaps

- Parallel dual execution with the real `DualScorer`, real `tests/test.sh`, and owned executor worktree isolation.
- Stale `repo_path/answer.json` fallback when the isolated worktree does not produce an artifact.
- Weighted policy with negative, over-1, non-finite, malformed, and sum-to-zero weights.
- Missing, malformed, or unreadable `metadata.json` in a dual scoring sandbox.
- Artifact-copy failure, partial write, and disappearing `answer.json` between discovery and copy.
- Ground-truth schema validation for `answer_type` and per-type answer payload shapes.
- `answer.json` `answer_type` mismatch against `ground_truth.json`.
- Reports and stats with mixed dual and non-dual configs plus low nonzero scores below `PASS_THRESHOLD`.

## Summary

The implementation still has correctness holes in the actual dual verifier boundary: the direct leg is not reliably bound to the per-run worktree, artifact discovery can consume stale shared files, and weighted scoring can be configured to pass failed artifact legs. The reporting and typed-detail layers also coerce or drop dual-leg state, so failures can be hidden after execution even when the scorer recorded them.
