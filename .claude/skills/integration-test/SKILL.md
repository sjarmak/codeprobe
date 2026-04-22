---
name: integration-test
description: Spawn a specialized subagent that installs codeprobe in a fresh virtual environment and dogfoods the full mine → run → interpret workflow against a real repo, capturing rich logs and producing a structured bug report. Use when unit tests are insufficient and you need an agent to drive the CLI end-to-end to surface integration bugs. Triggers on integration test, dogfood, end-to-end test, e2e, full workflow test, find bugs.
user-invocable: false
---

# Integration Test (Dogfood)

Our unit tests are insufficient — they mock too much and miss real integration bugs. This skill spawns a fresh subagent that installs codeprobe into an isolated virtual environment and drives the tool through its intended workflow (`mine` → `run` → `interpret`) against a real repository, capturing rich logs at every step and producing a structured bug report.

The subagent acts as a real user would: following the existing `/mine-tasks`, `/run-eval`, and `/interpret` skills, making realistic choices, and treating anomalies as bugs to investigate rather than obstacles to route around.

---

## Phase 0: Test Configuration

Ask the user:

**Question 1** — Header: "Target repo for mining"
- Question: "Which repo should the subagent mine tasks from?"
- Options:
  - **codeprobe itself** — "Dogfood against `/home/ds/projects/codeprobe` (has real merge history, fast)"
  - **Other local repo** — "I'll provide a path to a local git repo"
  - **Remote repo** — "I'll provide a GitHub/GitLab URL"

Set `TARGET_REPO` accordingly.

**Question 2** — Header: "Eval execution mode"
- Question: "How should the subagent execute the mined tasks?"
- Options:
  - **Dry-run only** — "Estimate cost without calling real agents (free, fastest)"
  - **Real eval, tiny budget** — "Run against a real agent with `--max-cost-usd 0.50`"
  - **Real eval, custom budget** — "I'll set a USD budget"

Set `EVAL_MODE` and `COST_FLAG`:
- Dry-run: `EVAL_MODE=dry-run`, `COST_FLAG="--dry-run"`
- Real, tiny: `EVAL_MODE=real`, `COST_FLAG="--max-cost-usd 0.50"`
- Real, custom: `EVAL_MODE=real`, `COST_FLAG="--max-cost-usd {amount}"`

If real eval, also ask which agent (`claude` or `copilot`) — default `claude`.

**Question 3** — Header: "Scope focus"
- Question: "Which parts of the workflow do you most want to stress? (determines what the subagent pays extra attention to)"
- Options:
  - **Full workflow** — "Exercise everything equally"
  - **Mining** — "Focus on task extraction quality, instruction generation, test script generation"
  - **Execution** — "Focus on adapter behavior, isolation, scoring, telemetry"
  - **Interpretation** — "Focus on report generation, ranking, cost accounting"

Set `FOCUS_AREA`.

### Pre-flight Summary

Display:

```
Integration test configuration:

  Target repo:  {TARGET_REPO}
  Eval mode:    {EVAL_MODE} ({COST_FLAG})
  Focus area:   {FOCUS_AREA}
  Workspace:    /tmp/codeprobe-inttest-<timestamp>/
  Source:       /home/ds/projects/codeprobe (editable install)

  Subagent will:
    1. Create fresh venv and pip install -e the local source
    2. Run codeprobe --version + doctor
    3. Mine 3 tasks from {TARGET_REPO}
    4. Validate/inspect the mined tasks
    5. Execute eval ({EVAL_MODE}) against mined tasks
    6. Run interpret on results
    7. Capture all stdout/stderr/JSON logs to workspace
    8. Return a structured bug report

Proceed?
```

Wait for confirmation.

---

## Phase 1: Spawn Subagent

Create the workspace directory and spawn a `general-purpose` subagent with the prompt template below. The prompt MUST be self-contained — the subagent has no prior context from this conversation.

```
WORKSPACE=/tmp/codeprobe-inttest-$(date +%Y%m%d-%H%M%S)
mkdir -p $WORKSPACE/logs
```

Spawn the subagent via the Agent tool with `subagent_type: general-purpose` and this prompt (fill in the `{{...}}` placeholders from Phase 0):

> **Prompt template — do not truncate when filling in.**
>
> You are running an integration test of the `codeprobe` Python package. Our unit tests mock too much and miss real bugs. Your job is to install codeprobe into a fresh virtual environment, drive it through its intended workflow as a real user would, capture rich logs, and produce a structured bug report.
>
> **Source code:** `/home/ds/projects/codeprobe` (this is an editable install target — do not modify source files). Architecture and CLI contract are documented in `/home/ds/projects/codeprobe/CLAUDE.md` and the skill docs under `/home/ds/projects/codeprobe/.claude/skills/{mine-tasks,run-eval,interpret}/SKILL.md`. Read these before executing to understand the intended UX.
>
> **Workspace:** `{{WORKSPACE}}` — put the venv at `$WORKSPACE/venv`, mined tasks at `$WORKSPACE/tasks`, results at `$WORKSPACE/results`, and ALL logs under `$WORKSPACE/logs/`.
>
> **Target repo for mining:** `{{TARGET_REPO}}`
> **Eval mode:** `{{EVAL_MODE}}` (pass `{{COST_FLAG}}` to `codeprobe run`)
> **Agent (if real eval):** `{{AGENT}}`
> **Focus area:** `{{FOCUS_AREA}}` — pay extra attention to bugs in this area, but test everything.
>
> ## Steps
>
> Execute the following in order. **Do not skip steps or work around failures** — failures are the whole point. When a command fails, capture the full error, form a hypothesis, and continue to the next step when possible so you can surface multiple bugs in one run.
>
> 1. **Venv + install.** Create `$WORKSPACE/venv` using `python3 -m venv`, activate it, and run `pip install -e /home/ds/projects/codeprobe 2>&1 | tee $WORKSPACE/logs/01-install.log`. Record pip's exit code. If install fails, stop and report (install bugs block everything else).
>
> 2. **Version + doctor.** Run `codeprobe --version` and `codeprobe doctor` with `2>&1 | tee $WORKSPACE/logs/02-doctor.log`. Record exit codes. Note any FAIL lines from doctor output. Missing API keys are expected in CI — note but don't block.
>
> 3. **Mine tasks.** Run mining with verbose + JSON logging:
>    ```
>    codeprobe -v --log-format json mine {{TARGET_REPO}} --count 3 --out $WORKSPACE/tasks 2>&1 | tee $WORKSPACE/logs/03-mine.log
>    ```
>    If the `--out` flag doesn't exist, read `codeprobe mine --help` and use the correct flag. Record the actual command used, its exit code, duration, and the list of task directories created.
>
> 4. **Inspect mined tasks.** For each task directory under `$WORKSPACE/tasks`, verify:
>    - `task.toml` exists and parses (try `python -c "import tomllib; tomllib.load(open('...', 'rb'))"`)
>    - `instruction.md` exists, is >50 chars, and does NOT leak ground truth (no answer strings, no "expected output: X" leaking the solution)
>    - `tests/test.sh` exists and is executable
>    - Required task.toml fields: `[task].id`, `[task].repo`, `[metadata].name`, `[verification].command`
>
>    Write findings to `$WORKSPACE/logs/04-inspect.json` as a list of `{task_id, path, issues: [...]}`.
>
> 5. **Validate (if command exists).** Try `codeprobe validate $WORKSPACE/tasks 2>&1 | tee $WORKSPACE/logs/05-validate.log`. If the command doesn't exist, note this and continue — it may be a missing feature.
>
> 6. **Run eval.** Execute the tasks:
>    ```
>    codeprobe -v --log-format json run $WORKSPACE/tasks --agent {{AGENT}} {{COST_FLAG}} 2>&1 | tee $WORKSPACE/logs/06-run.log
>    ```
>    For dry-run mode, confirm that the output includes a cost estimate and task breakdown. For real eval mode, confirm that each task produced a score, a cost, and a duration. Record the exit code.
>
> 7. **Interpret results.** Find the results directory created by the run (look under `$WORKSPACE/tasks` or current dir — note whatever codeprobe's convention actually is). Run:
>    ```
>    codeprobe interpret <results_path> --format text 2>&1 | tee $WORKSPACE/logs/07-interpret.log
>    codeprobe interpret <results_path> --format json > $WORKSPACE/logs/07-interpret.json 2>> $WORKSPACE/logs/07-interpret.log
>    ```
>    Verify the JSON output parses and contains sensible fields (config rankings, per-task scores, aggregate cost).
>
> 8. **Scan logs for anomalies.** Grep all `$WORKSPACE/logs/*` for: `Traceback`, `Error`, `ERROR`, `WARN`, `deprecated`, `not found`, `None` in places where it shouldn't be, `nan`/`null` in cost/score fields, and any assertion failures. Collate these into a list.
>
> ## What counts as a bug
>
> - Non-zero exit code on any step (except doctor with missing optional API keys)
> - Unhandled exceptions / tracebacks
> - Validation errors that break the workflow
> - Instruction files that leak ground truth
> - Missing telemetry: `None`, `null`, or `"unknown"` for cost_usd/tokens when the adapter should have captured them
> - `--help` documented flags that fail with "no such option"
> - Commands that hang past a reasonable timeout (>5 min without progress)
> - Output that disagrees with the skill docs (`mine-tasks/SKILL.md` etc.)
> - JSON log format producing invalid JSON
> - Silent data loss: empty results, missing task outputs, dropped telemetry
>
> **Not bugs:** missing API keys, network failures for remote repos you can't reach, `--dry-run` returning 0 real costs.
>
> ## Report format
>
> Return a markdown report with these sections. Be concrete — every bug must be reproducible from the report alone.
>
> ```
> # codeprobe Integration Test Report
>
> **Workspace:** {{WORKSPACE}}
> **Target repo:** {{TARGET_REPO}}
> **Eval mode:** {{EVAL_MODE}}
> **codeprobe version:** <from step 2>
> **Python version:** <from venv>
> **Overall status:** PASS | PARTIAL | FAIL
>
> ## Step Summary
>
> | # | Step | Exit | Duration | Status | Notes |
> |---|------|------|----------|--------|-------|
> | 1 | install | 0 | 12s | PASS | |
> | 2 | doctor | 0 | 1s | WARN | OPENAI_API_KEY not set |
> | ... | | | | | |
>
> ## Bugs Found
>
> For each bug:
>
> ### BUG-1: <one-line summary>
> - **Severity:** critical | high | medium | low
> - **Step:** <which phase surfaced it>
> - **Command:** `<exact command that triggered it>`
> - **Observed:** <what happened, with 10-20 lines of relevant log excerpt>
> - **Expected:** <what should have happened, with pointer to docs or skill>
> - **Hypothesis:** <your best guess at root cause, pointing at src/codeprobe/... file:line if possible>
> - **Repro:** <minimal command sequence to reproduce in a fresh venv>
>
> ## Anomalies / Questions
>
> Things that are suspicious but you couldn't confirm as bugs (e.g., cost of $0.0 on a real eval, task instructions that seemed thin, reports that lacked fields you expected).
>
> ## Log Inventory
>
> Absolute paths of every log file produced, so the main agent can cat them if needed.
> ```
>
> Keep the report under 6000 words. Quote log excerpts, don't paste entire files — the files live at `$WORKSPACE/logs/` and are available to the main agent.

Pass the filled-in prompt to the Agent tool. Do NOT run the subagent in the background — we need its report before Phase 2.

---

## Phase 2: Triage Report

When the subagent returns:

1. **Show the user the report** — display the full markdown as-is (it's already formatted).
2. **Highlight critical/high bugs** — repeat them in a short summary at the top of your response.
3. **Offer next actions**:
   - "Open `$WORKSPACE/logs/<file>` to see raw output for BUG-N"
   - "Create a bead for BUG-N" (use `bd create` — see project CLAUDE.md for the cold-start rule; beads MUST include exact file paths, steps, and acceptance criteria)
   - "Spawn the subagent again with a different target repo / focus area"
   - "Investigate BUG-N directly (read source, propose fix)"

Wait for the user to pick a next step. Do not auto-create beads — let the user decide which findings are worth tracking.

---

## Quick Reference

| User says | What happens |
|-----------|-------------|
| `/integration-test` | Full interactive flow, Phase 0 → 2 |
| "dogfood codeprobe" | Same as `/integration-test` |
| "run an e2e test against codeprobe itself" | Skip Phase 0 Q1, use `/home/ds/projects/codeprobe` |
| "integration test with dry-run only" | Skip Phase 0 Q2, use `--dry-run` |
| "find bugs in mining" | Set FOCUS_AREA=Mining |

---

## Notes

- The subagent is intentionally given latitude to discover bugs the test suite would miss. Do not over-constrain its prompt with fixed command lists beyond the numbered steps — the point is that it drives the CLI the way a real user would.
- The workspace is intentionally kept on disk after the run so findings can be inspected and reproduced. The user can `rm -rf /tmp/codeprobe-inttest-*` to clean up.
- This skill complements, not replaces, the pytest suite in `tests/`. Unit tests validate contracts; this skill validates that contracts compose into a working product.
