● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ 1 line read

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ 1 line read

● Read prd_dual_verifier_mining.md
  └ 199 lines read

● Read task.py
  │ src/codeprobe/models/task.py
  └ 150 lines read

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L1:100 (100 lines read)

● Read experiment.py
  │ src/codeprobe/models/experiment.py
  └ 74 lines read

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L1:100 (100 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L100:250 (151 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L100:250 (151 lines read)

● Read stats.py
  │ src/codeprobe/analysis/stats.py
  └ 455 lines read

● Read report.py
  │ src/codeprobe/analysis/report.py
  └ 1 line read

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L250:450 (201 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L250:500 (251 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L500:650 (151 lines read)

● Read validate_cmd.py
  │ src/codeprobe/cli/validate_cmd.py
  └ 288 lines read

● Read report.py
  │ src/codeprobe/analysis/report.py
  └ L1:100 (99 lines read)

● Read report.py
  │ src/codeprobe/analysis/report.py
  └ L100:300 (200 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L450:600 (151 lines read)

● Read events.py
  │ src/codeprobe/core/events.py
  └ 275 lines read

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L600:750 (151 lines read)

● Search (glob)
  │ "src/codeprobe/core/registry.py"
  └ 1 file found

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L750:900 (151 lines read)

● Read registry.py
  │ src/codeprobe/core/registry.py
  └ 114 lines read

Now I have everything I need. Let me compile the full adversarial review.

---

## Adversarial Review: Dual Verifier Mining PRD

### 🔴 CRITICAL — Will prevent execution

---

**C1: `"dual"` is not in the scorer registry**
- **Scenario:** User creates a dual task, runs `codeprobe run`. `execute_task()` calls `get_scorer(reward_type)` → `resolve_scorer("dual")`. `registry.py` lines 98–104 has `artifact`, `binary`, `continuous`, `checkpoint`, `test_ratio` — no `"dual"`. This throws `ValueError` and scores every dual task as 0.0 with status `"error"`. 
- **Location:** `src/codeprobe/core/registry.py:98-104`
- **Severity: Critical** — DualScorer cannot be invoked at all.

---

**C2: Executor auto-detection reads `reward_type`, not `verification_mode`**
- **Scenario:** R5 says "Executor detects `verification_mode="dual"` in task metadata and auto-selects DualScorer." But `executor.py:202-205` reads `(_task_meta.get("verification") or {}).get("reward_type")` — a different field. A task with `verification_mode="dual"` and `reward_type="binary"` (the default) will never trigger dual scoring. The only way to reach `DualScorer` is if `reward_type="dual"` is set, but that's not the field the PRD specifies as canonical.
- **Location:** `src/codeprobe/core/executor.py:199-205`
- **Severity: Critical** — R5 is unimplementable as spec'd without changing this logic.

---

**C3: Executor override only fires when experiment `reward_type == "binary"`**
- **Scenario:** If an operator runs a mixed experiment with `reward_type="continuous"` to handle org-scale tasks, the auto-detect block is skipped entirely (`if reward_type == "binary":`). Dual tasks in that experiment silently use `ContinuousScorer`, not `DualScorer`. R5's acceptance criterion says "regardless of experiment-level `reward_type`" — this code contradicts that directly.
- **Location:** `src/codeprobe/core/executor.py:202`
- **Severity: Critical** — Mixed experiments drop dual verification silently.

---

**C4: `answer.json` not in stale-file cleanup — the premortem's own Critical risk #1 is still live**
- **Scenario:** The PRD's premortem section (Sandbox Asymmetry, risk #1) identifies this exact bug and mandates R9-PM: "Add answer.json to stale-file cleanup list." Executor stale cleanup at lines 209-212 removes `answer.txt` and `reward.txt` — not `answer.json`. In sequential mode, task 1 writes `answer.json` to `task_dir`; task 2 (which produces no answer.json) gets task 1's artifact scored as its own. The design identifies this bug, declares it "Required mitigation," and then fails to include it.
- **Location:** `src/codeprobe/core/executor.py:209-212`
- **Severity: Critical** — Cross-task contamination in sequential mode.

---

**C5: Parallel mode races on shared `task_dir/answer.json`**
- **Scenario:** `executor.py:343-354` copies `answer.json` from the worktree into the *shared* `task_dir` (not the worktree). When two tasks with the same `task_dir` run in parallel (repeats > 1), both write to `task_dir/answer.json` concurrently. `ArtifactScorer._find_answer_file()` reads directly from `task_dir` without any locking. One repeat's answer.json overwrites the other's mid-scoring. Per-run isolation directory (R9-PM) would fix this, but it doesn't exist yet.
- **Location:** `src/codeprobe/core/executor.py:343-354` + `src/codeprobe/core/scoring.py:461-466`
- **Severity: Critical** — Parallel repeats produce nondeterministic scores.

---

### 🟠 HIGH — Produces wrong results or silently drops data

---

**H1: Sub-scores invisible in every report format**
- **Scenario:** Premortem risk #2 says "scoring_details flows through serialization but ConfigSummary, stats.py, and report.py never read it." This is still true. `report.py:_build_task_rows()` (lines 203-230) builds CSV/JSON rows with no `score_direct` or `score_artifact` columns. `format_text_report()` per-task table (lines 171-184) has columns: Score, Pass, Duration, Cost — no sub-scores. The HTML report won't show them either. A DualScorer could populate `scoring_details` perfectly, and all four output formats would still show only `automated_score`. R11-PM's E2E test is supposed to catch this, but that test doesn't exist.
- **Location:** `src/codeprobe/analysis/report.py:203-230, 171-184` + `src/codeprobe/analysis/stats.py` (entire file — no dual awareness)
- **Severity: High** — Artifact dimension silently vanishes from all output.

---

**H2: `dual_composite()` doesn't exist**
- **Scenario:** R8 mandates `dual_composite(task, strategy="min")` in `analysis/stats.py` or `analysis/dual.py`. Neither file contains this function. Any report code or CLI that calls it gets `AttributeError` at runtime. `ConfigSummary` has no `score_direct` or `score_artifact` field. Report generation has no dual-awareness path whatsoever.
- **Location:** `src/codeprobe/analysis/stats.py` (entire file)
- **Severity: High** — Required analysis helper missing entirely.

---

**H3: `DualScoringDetails` frozen dataclass doesn't exist**
- **Scenario:** R10-PM mandates `DualScoringDetails` with typed fields `score_direct`, `score_artifact`, `passed_direct`, `passed_artifact`, `scoring_policy`, `extra`. `CompletedTask.scoring_details` is `dict` (experiment.py:54). The premortem's entire rationale for R10-PM is that untyped dicts cause silent data loss — the analysis code skips keys it doesn't know about. Since `DualScoringDetails` doesn't exist, static analysis can't catch missing field references, and the mitigation for risk #2 is unfulfilled.
- **Location:** `src/codeprobe/models/experiment.py:54`
- **Severity: High** — Type safety mitigation entirely absent.

---

**H4: `scoring_policy` has no model field — R3 unimplementable as stated**
- **Scenario:** R3 mandates `task.toml` with `[verification] scoring_policy = "min"` controlling `automated_score`. But `TaskVerification` dataclass (task.py:119-136) has no `scoring_policy`, `weight_direct`, or `weight_artifact` fields. TOML parsing in `validate_cmd.py` would load the data, but nothing would read the `scoring_policy` key. There's no dispatch to min/mean/weighted anywhere in the scoring or executor path.
- **Location:** `src/codeprobe/models/task.py:119-136`
- **Severity: High** — R3 requires a model field that doesn't exist.

---

**H5: `_compute_f1` returns 0.0 when expected is empty — unscoreable "no-change" oracles**
- **Scenario:** `scoring.py:578`: `if not expected_set: return 0.0`. If a PR oracle has zero expected files (e.g., all changed files were test files and were filtered out per R16's constraint), the task is unscorable — any agent answer, including a correct empty answer, gets F1=0.0. The design plans to auto-generate oracles from PR diffs while excluding test files. PRs that only touch tests produce empty `expected_set`, making those tasks permanently broken. The PRD doesn't acknowledge this edge case.
- **Location:** `src/codeprobe/core/scoring.py:578`
- **Severity: High** — Test-only PRs produce broken oracles that can never be passed.

---

**H6: `TaskScored` event has no sub-score fields — R15 impossible**
- **Scenario:** R15 requires CLI output like `task-id: 0.70 (code:PASS artifact:0.40)`. The `TaskScored` event (events.py:44-60) carries only `automated_score: float`. Listeners implementing R15 would need to read `scoring_details`, but the event doesn't carry it. To add sub-scores to `TaskScored`, the frozen dataclass needs new fields — a breaking protocol change for any existing listener.
- **Location:** `src/codeprobe/core/events.py:44-60`
- **Severity: High** — R15 requires a protocol change not acknowledged in the PRD.

---

**H7: `VERIFICATION_MODES` contains "dual" but executor/registry don't handle it — false validation**
- **Scenario:** `codeprobe validate` on a dual task passes (`validation_mode valid`, `tests/test.sh`, `tests/ground_truth.json` all checked). But `get_scorer("dual")` raises `ValueError`. Validation explicitly says the task is valid; execution silently fails. This creates a false positive validation signal that will mislead task authors.
- **Location:** `src/codeprobe/models/task.py:67-73` + `src/codeprobe/core/registry.py:98-104`
- **Severity: High** — Validate says pass; run says error. Contradictory signals.

---

### 🟡 MEDIUM — Correctness or design defects

---

**M1: `answer_type="text"` uses case-normalized exact match — comprehension tasks will fail trivially**
- **Scenario:** `scoring.py:609`: `_score_text = _score_exact_match` which does `str(expected).strip().lower() == str(actual).strip().lower()`. A dual task asking "What is the purpose of this module?" with expected answer `"Handles HTTP request routing"` will score 0.0 if the agent answers `"Handles HTTP request routing."` (trailing period) or `"It handles HTTP request routing"`. For natural language comprehension answers (the primary Phase 1 use case), exact match produces near-zero pass rates.
- **Location:** `src/codeprobe/core/scoring.py:603-609`
- **Severity: Medium** — Comprehension leg will fail almost all responses with natural language answers.

---

**M2: CheckpointScorer returns early on first missing verifier — bad precedent for DualScorer**
- **Scenario:** R6 mandates "Both sub-scorers execute even if the first one fails." But `CheckpointScorer.score()` (scoring.py:396-402) returns `ScoreResult(score=0.0, error="Verifier not found")` immediately if *any* verifier is missing — it doesn't run the remaining verifiers. Since DualScorer is modeled on the existing scorer pattern, implementors reading `CheckpointScorer` will likely copy this early-return pattern. R6's requirement directly contradicts the established implementation precedent.
- **Location:** `src/codeprobe/core/scoring.py:396-402`
- **Severity: Medium** — Likely to produce wrong DualScorer implementation by analogy.

---

**M3: Parallel budget cancellation doesn't kill running agent subprocesses**
- **Scenario:** `executor.py:878-885`: `f.cancel()` only cancels futures not yet started. Already-running `execute_task()` calls (which themselves run agent subprocesses for up to 300+ seconds) continue until they complete. With `parallel=8` and 7 tasks already running when budget is exceeded, the budget can be multiplied by 7x before halting. The comment says "halting" but it's a lie.
- **Location:** `src/codeprobe/core/executor.py:878-885`
- **Severity: Medium** — Budget overshoot in parallel mode is proportional to parallelism.

---

**M4: `_find_answer_file` second candidate is dead code**
- **Scenario:** `scoring.py:463` checks `task_dir / "tests" / "answer.json"`. But `execute_task()` always copies answer.json to `task_dir / "answer.json"` (line 351), never to `task_dir / "tests" / "answer.json"`. The second candidate is never populated by the executor — it only exists to handle manually-placed files, which is undocumented behavior.
- **Location:** `src/codeprobe/core/scoring.py:461-466`
- **Severity: Medium** — Dead code that could cause confusion about canonical answer.json location.

---

**M5: `repeats + dual` variance is irreparably mixed — open question dismissed too lightly**
- **Scenario:** PRD open question #2: "If test.sh is deterministic but artifact answers vary across runs, variance analysis mixes signals." The PRD dismisses this as "lower priority." But `compare_configs()` (stats.py:382-455) computes variance across all scores for statistical tests. For dual tasks with `repeats > 1`, `automated_score = score_direct` is deterministic (test.sh either passes or doesn't), but `score_artifact` varies. The statistics (McNemar's, Wilcoxon) will show zero variance even when artifact behavior is highly variable. Dual tasks in repeated experiments produce artificially low p-values.
- **Location:** `src/codeprobe/analysis/stats.py:382-455`
- **Severity: Medium** — Silent statistical bias in repeated dual experiments.

---

### 🔵 LOW — Design inconsistencies

---

**L1: Validate doesn't check `scoring_policy` validity**
- **Scenario:** A task with `scoring_policy = "typo"` passes `codeprobe validate` but would produce a runtime error (or silently fall back) when DualScorer tries to dispatch on the policy string. The `run_validate()` function (validate_cmd.py:222-256) checks verification_mode and task_type against frozensets, but there's no frozenset for valid scoring policies.
- **Location:** `src/codeprobe/cli/validate_cmd.py:222-256`
- **Severity: Low** — Missing validation for a new field.

---

**L2: R12-PM's 20+ pre-curated tasks are Must-Have but `examples/dual/` doesn't exist**
- **Scenario:** The premortem explicitly moved "ship 20+ pre-curated dual tasks" to Phase 1 Must-Have (R12-PM), acknowledging that zero external adoption would otherwise occur. The directory doesn't exist. This isn't a code bug — it's a process failure. But the premortem's own kill criterion ("if <10 external dual tasks in 60 days, escalate") starts the clock from zero.
- **Location:** Missing `examples/dual/`
- **Severity: Low** (process) — Phase 1 delivers zero demonstrable value without this.

---

## Summary Table

| ID | Issue | Severity | PRD Req |
|----|-------|----------|---------|
| C1 | `"dual"` missing from scorer registry | **Critical** | R2 |
| C2 | Executor reads `reward_type` not `verification_mode` | **Critical** | R5 |
| C3 | Auto-detect skipped when experiment `reward_type != "binary"` | **Critical** | R5 |
| C4 | `answer.json` not in stale-file cleanup | **Critical** | R9-PM |
| C5 | Parallel repeats race on shared `task_dir/answer.json` | **Critical** | R9-PM |
| H1 | Sub-scores invisible in all 4 report formats | High | R11-PM |
| H2 | `dual_composite()` doesn't exist | High | R8 |
| H3 | `DualScoringDetails` dataclass doesn't exist | High | R10-PM |
| H4 | `scoring_policy` not in data model | High | R3 |
| H5 | F1=0.0 for empty oracle — test-only PRs unscoreable | High | R16 |
| H6 | `TaskScored` event carries no sub-scores | High | R15 |
| H7 | Validate passes dual tasks that execution will fail | High | R7 |
| M1 | Exact-match text scoring breaks comprehension answers | Medium | R1 |
| M2 | `CheckpointScorer` early-return anti-pattern | Medium | R6 |
| M3 | Budget `f.cancel()` doesn't kill running subprocesses | Medium | (existing) |
| M4 | `tests/answer.json` candidate is dead code | Medium | R4 |
| M5 | Repeated dual experiments have silent statistical bias | Medium | (gap) |
| L1 | Validate ignores `scoring_policy` validity | Low | R7 |
| L2 | `examples/dual/` doesn't exist | Low | R12-PM |

**Fatal combination:** C1+C2+C3 means `DualScorer` can never be invoked through any normal code path. C4+C5 means even if it were, sequential and parallel execution both produce contaminated scores. H1+H2+H3 mean even correct scores would be invisible to users. The Phase 1 deliverable as written cannot function end-to-end.


Changes   +0 -0
Requests  1 Premium (2m 54s)
Tokens    ↑ 476.3k • ↓ 8.8k • 381.9k (cached)
