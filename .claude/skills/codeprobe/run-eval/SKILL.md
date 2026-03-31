---
name: run-eval
description: Run eval tasks against an AI coding agent. Spawns isolated agent sessions for each task, scores results with automated tests, and produces a results summary. Supports Claude and Copilot agents, model overrides, evalrc configs, and cost budgets. Triggers on run eval, execute eval, run tasks, run benchmark, evaluate tasks, score tasks, test my agent.
user-invocable: true
---

# Run Eval

Run eval tasks against an AI coding agent. Each task is executed in an isolated agent session that only sees the task instruction -- never the ground truth or scoring rubric. Results are scored with automated tests and summarized.

Invokes `codeprobe run` under the hood -- all execution runs through the CLI, not Python imports.

Works with tasks produced by `codeprobe mine` (the `/mine-tasks` skill) or manually authored tasks.

---

## Phase 0: Run Configuration

Ask the user:

**Question 1** -- Header: "Task source"
- Question: "Where are the eval tasks?"
- Options:
  - **Current directory** -- "Look for tasks in the current working directory"
  - **Specific path** -- "I'll provide a path to a task directory or evalrc config"

If **Current directory**, set `REPO_PATH=.`.
If **Specific path**, prompt for the path and set `REPO_PATH={user_input}`.

**Question 2** -- Header: "Agent"
- Question: "Which AI coding agent should I evaluate?"
- Options:
  - **Claude** -- "Anthropic's Claude Code CLI agent"
  - **Copilot** -- "GitHub Copilot CLI agent"

Map to `AGENT`:
- Claude: `--agent claude`
- Copilot: `--agent copilot`

**Question 3** -- Header: "Model"
- Question: "Which model should the agent use?"
- Options:
  - **Default** -- "Use the agent's default model"
  - **Specific model** -- "I'll specify a model (e.g., claude-sonnet-4-6, claude-opus-4-6)"

If **Default**, set `MODEL_FLAG=""`.
If **Specific model**, prompt for the model name and set `MODEL_FLAG="--model {model_name}"`.

**Question 4** -- Header: "Configuration"
- Question: "Use a custom experiment configuration?"
- Options:
  - **None** -- "Run with defaults"
  - **Evalrc file** -- "I have a .evalrc.yaml or experiment directory to use"

If **None**, set `CONFIG_FLAG=""`.
If **Evalrc file**, prompt for the path and set `CONFIG_FLAG="--config {config_path}"`.

**Question 5** -- Header: "Cost budget"
- Question: "Set a maximum cost budget? (prevents runaway spending)"
- Options:
  - **No limit** -- "Run until all tasks complete"
  - **Set budget** -- "I'll specify a max cost in USD"

If **No limit**, set `COST_FLAG=""`.
If **Set budget**, prompt for the dollar amount and set `COST_FLAG="--max-cost-usd {amount}"`.

### Pre-flight Summary

Before running, display the configuration:

```
Run configuration:

  Path:    {REPO_PATH}
  Agent:   {AGENT}
  Model:   {MODEL or "default"}
  Config:  {CONFIG or "none"}
  Budget:  {COST or "unlimited"}

  Command: codeprobe run {REPO_PATH} {AGENT} {MODEL_FLAG} {CONFIG_FLAG} {COST_FLAG}

Proceed?
```

Wait for confirmation before executing.

---

## Phase 1: Execute Eval

Run the codeprobe CLI:

```bash
codeprobe run {REPO_PATH} --agent {AGENT} {MODEL_FLAG} {CONFIG_FLAG} {COST_FLAG}
```

This:
1. Discovers tasks in the target directory
2. Validates task structure (instruction files, test scripts)
3. Spawns isolated agent sessions for each task
4. Scores results with automated test scripts
5. Aggregates results into a summary

### Cost Guard

If `--max-cost-usd` is set, the CLI will halt execution when cumulative cost reaches the budget. Partial results are preserved.

---

## Phase 2: Present Results

Display the eval results. For each task, show:

```
Eval Results:

| # | Task ID              | Score | Cost    | Time   | Status |
|---|----------------------|-------|---------|--------|--------|
| 1 | repo-leak-fix-001    | 1.00  | $0.23   | 3m12s  | pass   |
| 2 | repo-auth-feat-001   | 0.50  | $0.45   | 5m30s  | partial|
| 3 | repo-refactor-001    | 0.00  | $0.12   | 1m45s  | fail   |

Summary:
  Mean score:    0.50
  Total cost:    $0.80
  Total time:    10m27s
  Pass rate:     1/3 (33%)
```

Highlight:
- **Overall performance** -- Mean score, pass rate
- **Cost efficiency** -- Score per dollar
- **Failures** -- Which tasks failed and why (test output excerpts)

---

## Phase 3: Next Steps

```
Eval complete. Next steps:

  1. Interpret results:
     codeprobe interpret {results_path}

  2. Compare with a different model:
     codeprobe run {REPO_PATH} --agent claude --model claude-opus-4-6

  3. Compare with a different agent:
     codeprobe run {REPO_PATH} --agent copilot

  4. Mine more tasks for better coverage:
     codeprobe mine {REPO_PATH} --count 15

  5. Set up a multi-config experiment with .evalrc.yaml
     for systematic comparison across models and tools.
```

---

## Quick Reference

| User says | What happens |
|-----------|-------------|
| `/run-eval` | Run eval from current directory, interactive Q&A |
| `/run-eval /path/to/tasks` | Run eval from specific path |
| "run eval with claude" | Run with `--agent claude` |
| "benchmark with opus" | Run with `--agent claude --model claude-opus-4-6` |
| "run eval with $5 budget" | Run with `--max-cost-usd 5.00` |
| "evaluate my agent" | Same as `/run-eval` |
