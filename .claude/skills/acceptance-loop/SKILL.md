---
name: acceptance-loop
description: Orchestrate the continuous Test→Verify→Fix→Release acceptance loop for codeprobe. Spawns a Test Agent to produce a workspace, runs the Verifier to produce verdict.json, feeds verdicts into the convergence controller, spawns a Fix Agent when failures remain, runs the regression gate after every fix, and promotes to the release gate after two consecutive green verdicts. Triggers on acceptance loop, convergence loop, test verify fix, /acceptance-loop.
user-invocable: true
---

# Acceptance Loop: Continuous Test→Verify→Fix→Release

## Purpose

Drive codeprobe toward a releasable state by repeatedly spawning a Test Agent to exercise the tool, running the behavioral Verifier against the produced workspace, and spawning a Fix Agent when the verdict contains failures. Every fix is gated by `acceptance/regression.py` (pytest + ruff + mypy with auto-revert), every verdict is fed into `acceptance/converge.py` for a deterministic CONTINUE / HALT / RELEASE / ESCALATE decision, and release promotion is gated by `acceptance/release.py` (wheel build + staged smoke test + version bump + tag). The loop is ZFC-compliant: all policy decisions are structured-data policy, not model judgment.

This SKILL is the single entry point. Sub-skills for spawning each agent live in [`test-agent.md`](./test-agent.md) and [`fix-agent.md`](./fix-agent.md) — do **not** inline their prompts here; read them from disk and substitute parameters.

---

## Parameters

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `target_repo` | yes | — | Absolute path to the frozen test repo the Test Agent exercises. |
| `pinned_sha` | yes | — | Expected git SHA of `target_repo`. Mismatch halts the loop before iteration 1. |
| `max_iterations` | no | `5` | Hard cap on loop iterations. Passed to `ConvergenceController(max_iterations=...)`. |
| `eval_mode` | no | `dry-run` | `dry-run` (no agent calls) or `real` (cost-bounded). Forwarded to the Test Agent. |
| `repo_root` | no | `/home/ds/projects/codeprobe` | codeprobe repo the Fix Agent edits. Also the regression-gate target. |

Reject the invocation if `target_repo` or `pinned_sha` are missing — no interactive prompting; this skill assumes it is invoked programmatically by `/acceptance-loop` with fully-bound parameters.

---

## Phase 0: Configure

### 0.1 Parse and validate parameters

Bind the parameters above into shell variables (`TARGET_REPO`, `PINNED_SHA`, `MAX_ITERATIONS`, `EVAL_MODE`, `REPO_ROOT`). Fail fast with a `FAILURE: <reason>` line if any required value is missing or non-absolute.

### 0.2 Stale workspace cleanup

Remove any `/tmp/codeprobe-loop-*` directory older than 24 hours so long-running sessions don't fill `/tmp`:

```bash
find /tmp -maxdepth 1 -type d -name 'codeprobe-loop-*' -mtime +1 -print -exec rm -rf {} +
```

### 0.3 Disk space pre-check

Refuse to start if `/tmp` has less than 2 GB free — the Test Agent captures full CLI output and the wheel staging step creates a venv:

```bash
FREE_KB=$(df -Pk /tmp | awk 'NR==2 {print $4}')
if [ "$FREE_KB" -lt 2097152 ]; then
  echo "FAILURE: /tmp has <2GB free ($FREE_KB KB); aborting acceptance loop"
  exit 1
fi
```

### 0.4 Concurrent-run lock (git tag)

Use a local-only git tag `codeprobe-loop-running` as a mutex. Stale locks older than 4 hours are auto-removed; fresh locks block. The tag is removed in the Cleanup section regardless of how the loop exits:

```bash
cd "$REPO_ROOT"
if git rev-parse -q --verify refs/tags/codeprobe-loop-running >/dev/null; then
  LOCK_EPOCH=$(git log -1 --format=%ct refs/tags/codeprobe-loop-running 2>/dev/null || echo 0)
  AGE=$(( $(date +%s) - LOCK_EPOCH ))
  if [ "$AGE" -gt 14400 ]; then
    git tag -d codeprobe-loop-running
  else
    echo "FAILURE: another acceptance-loop run holds codeprobe-loop-running (age ${AGE}s)"
    exit 1
  fi
fi
git tag codeprobe-loop-running
```

### 0.5 Loop workspace root

```bash
LOOP_ROOT=/tmp/codeprobe-loop-$(date +%Y%m%d-%H%M%S)
mkdir -p "$LOOP_ROOT"
CONVERGE_DB="$LOOP_ROOT/converge.db"
VERDICT_HISTORY=()
```

Each iteration gets its own subdirectory `$LOOP_ROOT/iter-<N>/` that the Test Agent uses as its workspace and that holds that iteration's `verdict.json`.

---

## Phase 1: Test & Verify (per iteration)

For each `ITER` in `1..MAX_ITERATIONS`:

### 1.1 Per-iteration workspace

```bash
WORKSPACE="$LOOP_ROOT/iter-$ITER"
mkdir -p "$WORKSPACE"
```

### 1.2 Spawn the Test Agent sub-agent

Read `./.claude/skills/acceptance-loop/test-agent.md`, substitute the four `{{PARAM}}` tokens (`{{ITERATION}}`, `{{TARGET_REPO}}`, `{{PINNED_SHA}}`, `{{EVAL_MODE}}`), and hand the bound prompt to a `general-purpose` sub-agent via the Agent tool. Also pass `{{WORKSPACE}} = $WORKSPACE` if the sub-skill references it.

Wait for the sub-agent to exit. It MUST produce `$WORKSPACE/workspace-manifest.json`. If the manifest is missing, jump to the ESCALATE handler with reason `test_agent_no_manifest`.

### 1.3 Run the Verifier

The verifier has no argparse CLI, so drive it via a one-shot `python3 -c` that imports `Verifier`, runs it, and writes the verdict to `$WORKSPACE/verdict.json`:

```bash
python3 -c "
import pathlib
from acceptance.verify import Verifier
v = Verifier(pathlib.Path('$REPO_ROOT/acceptance/criteria.toml'),
             project_root=pathlib.Path('$REPO_ROOT'))
verdict = v.run(pathlib.Path('$WORKSPACE'), iteration=$ITER)
v.write_verdict(verdict, pathlib.Path('$WORKSPACE/verdict.json'))
" || { echo 'FAILURE: verifier crashed'; exit 3; }
VERDICT_HISTORY+=("$WORKSPACE/verdict.json")
```

### 1.4 Record the verdict with the convergence controller

```bash
python3 -c "
import json, pathlib
from acceptance.converge import ConvergenceController
cc = ConvergenceController(pathlib.Path('$CONVERGE_DB'), max_iterations=$MAX_ITERATIONS)
cc.record_verdict(json.loads(pathlib.Path('$WORKSPACE/verdict.json').read_text()))
"
```

### 1.5 Ask for the decision

```bash
DECISION=$(python3 -c "
import pathlib
from acceptance.converge import ConvergenceController
cc = ConvergenceController(pathlib.Path('$CONVERGE_DB'), max_iterations=$MAX_ITERATIONS)
print(cc.decide().decision.value)
")
```

Branch on `$DECISION`:
- `release` → jump to **Phase 3: Release**.
- `continue` → proceed to **Phase 2: Fix** (if the verdict has failures) or loop back to 1.1 with `ITER++`.
- `halt_max_iterations` | `halt_regression` | `halt_stuck` | `escalate` → jump to **Halt Conditions**.

---

## Phase 2: Fix (conditional)

Only entered when `$DECISION == continue` AND the verdict has `fail_count > 0`. If `fail_count == 0` but the controller still says `continue`, skip directly to the next iteration (the loop is waiting for the second green in a row).

### 2.1 Spawn the Fix Agent sub-agent

Read `./.claude/skills/acceptance-loop/fix-agent.md`, substitute its parameters (`{{ITERATION}}`, `{{REPO_ROOT}}`, `{{VERDICT_PATH}}`), and hand the bound prompt to a fresh `general-purpose` sub-agent. The Fix Agent is contractually constrained to produce exactly ONE commit or print `FAILURE: <criterion_id>` on stdout.

### 2.2 Regression gate after every fix

Run the regression gate against `$REPO_ROOT`. It pytests, ruffs, mypys, and auto-reverts HEAD on failure:

```bash
python3 -m acceptance.regression --repo-root "$REPO_ROOT"
RC=$?
if [ $RC -ne 0 ]; then
  echo "regression gate FAILED at iteration $ITER — commit reverted"
fi
```

A regression-gate failure is **not** an automatic halt — the Test Agent re-runs on the reverted tree in the next iteration. The convergence controller halts the loop on its own via `HALT_REGRESSION` if `pass_count` drops between consecutive verdicts.

### 2.3 Loop

`ITER=$((ITER+1))` and jump back to **Phase 1: Test & Verify**. Do not clear `$CONVERGE_DB` — it is the source of truth for the two-green-in-a-row release check.

---

## Phase 3: Release (conditional)

Entered exactly once when `cc.decide() == Decision.RELEASE`. Release is all-or-nothing: any sub-step failure aborts with an escalation report, leaves the lock tag in place until Cleanup, and returns non-zero.

```bash
python3 -c "
import pathlib, sys
from acceptance.release import ReleaseGate
gate = ReleaseGate(pathlib.Path('$REPO_ROOT'))
verdicts = [pathlib.Path(p) for p in '''${VERDICT_HISTORY[@]}'''.split()]
if not gate.check_ready(verdicts):
    print('FAILURE: release gate refused — verdict history not ready'); sys.exit(2)
staging = gate.build_and_stage()
if staging.error:
    print(f'FAILURE: staging failed — {staging.error}'); sys.exit(3)
new_version = gate.bump_version('patch')
tag = gate.prepare_tag(new_version)
print(f'RELEASE_READY version={new_version} tag={tag}')
" || { echo 'release gate failed'; exit 4; }
```

Show the user the `RELEASE_READY` line plus the staged wheel path. The actual `git push --tags` is a human action — this loop stops at "tag prepared locally".

---

## Halt Conditions

When `cc.decide()` returns a non-CONTINUE/non-RELEASE decision, render and surface the escalation report before cleaning up:

```bash
python3 -c "
import pathlib
from acceptance.converge import ConvergenceController
cc = ConvergenceController(pathlib.Path('$CONVERGE_DB'), max_iterations=$MAX_ITERATIONS)
print(cc.get_escalation_report())
" > "$LOOP_ROOT/escalation.md"
cat "$LOOP_ROOT/escalation.md"
```

Decision-specific user messaging:

- **`halt_max_iterations`** — Loop cap hit without reaching two-green. Report iteration count, latest `pass_count/fail_count`, and the escalation markdown. Exit code 10.
- **`halt_regression`** — A fix made things worse (`pass_count` dropped). Report the regression delta from the decision context. The offending commit was already reverted by the regression gate; point the user at `$LOOP_ROOT/iter-<N>/verdict.json`. Exit code 11.
- **`halt_stuck`** / **`escalate`** — Three-strike rule triggered: same criterion failed 3 iterations in a row with identical evidence. Report the stuck criterion IDs, their evidence, and recommend human investigation. Exit code 12.

In every halt path, preserve `$LOOP_ROOT` for post-mortem — do NOT delete it in Cleanup.

---

## Cleanup

Always executed, even on failure, via a `trap` at the top of the loop or an explicit final block:

1. Remove the concurrent-run lock: `cd "$REPO_ROOT" && git tag -d codeprobe-loop-running 2>/dev/null || true`.
2. On RELEASE or max-iterations-green paths, optionally prune `$LOOP_ROOT` — otherwise preserve it and print the path so the user can inspect iteration workspaces and `escalation.md`.
3. Print a one-line summary: `acceptance-loop done: iterations=N decision=$DECISION workspace=$LOOP_ROOT`.

---

## References

- `acceptance/criteria.toml` — 25 seed criteria in TOML.
- `acceptance/loader.py::load_criteria()` — parsed into `Criterion` objects.
- `acceptance/verify.py::Verifier.run()` / `.write_verdict()` — produces `verdict.json`.
- `acceptance/converge.py::ConvergenceController` — `record_verdict`, `decide`, `is_release_ready`, `get_escalation_report`.
- `acceptance/regression.py` — `python3 -m acceptance.regression --repo-root <path>`.
- `acceptance/release.py::ReleaseGate` — `check_ready`, `build_and_stage`, `bump_version`, `prepare_tag`.
- [`test-agent.md`](./test-agent.md) — Test Agent sub-skill prompt (do not inline).
- [`fix-agent.md`](./fix-agent.md) — Fix Agent sub-skill prompt (do not inline).
