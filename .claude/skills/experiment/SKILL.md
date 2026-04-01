---
name: experiment
description: Guided entry point for setting up eval experiments. Walks users through defining what they want to learn, mining or selecting tasks, configuring comparisons (models, tools, prompts), and interpreting results. Orchestrates mine-tasks, run-eval, and interpret skills. Triggers on experiment, new experiment, compare models, compare configurations, set up experiment, set up eval, benchmark.
user-invocable: true
---

# Experiment

Set up, run, and interpret an eval experiment. An experiment compares one or more agent configurations (model, tools, prompts) across a set of real coding tasks mined from your codebase.

Invokes `codeprobe experiment` under the hood -- all management runs through the CLI, not Python imports.

This skill is the guided entry point. It creates an experiment directory with a configuration matrix, then delegates to `/mine-tasks`, `/run-eval`, and `/interpret` for execution and analysis.

**Users do not need to understand benchmarks or agentic workflows to use this.** The skill asks plain-language questions and maps answers to the right technical setup.

---

## Phase 0: What Do You Want to Learn?

Start with the user's question, not with implementation details.

### Step 0a: Goal

**Question** -- Header: "What are you trying to find out?"

Present these options with plain-language descriptions:

| # | Option | Description |
|---|--------|-------------|
| 1 | **Does an MCP tool help my agent?** | Compare your agent with and without an MCP server (like Sourcegraph). See if MCP-backed code search makes the agent faster or more accurate. |
| 2 | **Which model works best for my codebase?** | Run the same tasks on different models (e.g., Sonnet vs Opus) to see which handles your code best. |
| 3 | **How do different prompts affect results?** | Test different instruction styles or system prompts to find what gets the best agent behavior. |
| 4 | **Custom comparison** | Full control -- define exactly what varies between configurations. |
| 5 | **I already have tasks, just run them** | Skip task mining. Point to existing tasks and set up configurations. |

Record the user's goal as `EXPERIMENT_GOAL`. This determines which questions to ask next and which defaults to pre-fill.

### Step 0b: Experiment Name

**Question** -- Header: "Name your experiment"

Auto-suggest based on goal:

- Goal 1 (MCP) -> `{repo-name}-mcp-comparison`
- Goal 2 (models) -> `{repo-name}-model-comparison`
- Goal 3 (prompts) -> `{repo-name}-prompt-comparison`
- Goal 4 (custom) -> `{repo-name}-eval`
- Goal 5 (existing tasks) -> `{task-dir-name}-eval`

Let the user accept or change the name. Record as `EXPERIMENT_NAME`.

### Step 0c: Task Source

**If Goal is 1-4** (needs tasks):

**Question** -- Header: "Where should the tasks come from?"

| # | Option | Description |
|---|--------|-------------|
| 1 | **Mine from a repo** | Point at a repo and automatically extract real coding tasks from merged PRs. Takes ~5-10 minutes. |
| 2 | **Use existing tasks in this directory** | I already have task directories (with `instruction.md` and `task.toml` files). |
| 3 | **Point me to a task directory** | I'll specify the path. |

If **Mine from a repo**: record `TASK_SOURCE=mine`. Will delegate to `/mine-tasks` in Phase 2.
If **Use existing tasks**: scan current directory for task directories (contain `instruction.md` or `task.toml`). List what was found. Record `TASK_SOURCE=existing`.
If **Point me to a directory**: ask for path. Record `TASK_SOURCE=path`.

**If Goal is 5** (existing tasks): Go directly to the scan/path flow above.

### Step 0d: Task Count Guidance (only if TASK_SOURCE=mine)

**Question** -- Header: "How thorough should the evaluation be?"

| # | Option | Tasks | Description |
|---|--------|-------|-------------|
| 1 | **Quick look** | 3-5 | Fast results. Good for a first experiment or validating your setup works. |
| 2 | **Standard** | 5-10 | Good balance of coverage and speed. Enough tasks to see patterns. |
| 3 | **Thorough** | 10-20 | More statistical confidence in the results. Best for making real tooling decisions. |

Record as `TASK_COUNT_TARGET`.

---

## Phase 1: Configure Comparisons

Build the configuration matrix. The questions depend on `EXPERIMENT_GOAL`.

### Step 1a: Pre-fill from Goal

**Goal 1 (MCP comparison):**
Pre-create 2 configurations:

| Config | Label | Agent | Model | MCP |
|--------|-------|-------|-------|-----|
| A | `baseline` | (same) | (same) | none |
| B | `with-mcp` | (same) | (same) | (ask which provider) |

Then ask:
- **Question**: "Which agent?" -- Claude (default) or Copilot
- **Question**: "Which model?" -- Default: `claude-sonnet-4-6`
- **Question**: "Which MCP provider?" -- Sourcegraph / Custom / I'll configure later

**Goal 2 (model comparison):**
- **Question**: "Which models do you want to compare?" -- Let user pick multiple.
- Pre-create one configuration per selected model.

**Goal 3 (prompt comparison):**
- **Question**: "How many instruction variants?" -- Enter a number.
- For each, ask for a label and instruction file.

**Goal 4/5 (custom or existing tasks):**
- **Question**: "How many configurations?" -- Minimum 1.
- Collect all fields per configuration.

### Step 1b: Per-Configuration Details

For each configuration not already pre-filled:

- **Label** -- unique within experiment, used as directory name
- **Agent** -- `claude` (default) or `copilot`
- **Model** -- `claude-sonnet-4-6`, `claude-opus-4-6`, `claude-haiku-4-5`, or custom
- **MCP config** -- None, Sourcegraph, or custom JSON

### Step 1c: Confirm Matrix

Present the complete configuration matrix:

```
Your experiment: "{EXPERIMENT_NAME}"

Configurations:
| # | Label | Agent | Model | MCP |
|---|-------|-------|-------|-----|
| 1 | baseline | claude | claude-sonnet-4-6 | none |
| 2 | with-mcp | claude | claude-sonnet-4-6 | Sourcegraph |

Tasks: {N} ({TASK_SOURCE})
Total runs: {N tasks} x {M configs} = {N*M}
```

**Question** -- "Ready to proceed, or want to adjust?"

### Step 1d: Create Experiment Directory

```bash
codeprobe experiment init {PATH} --name "{EXPERIMENT_NAME}" \
  --description "{EXPERIMENT_GOAL description}"
```

For each configuration:

```bash
codeprobe experiment add-config {PATH} \
  --label "{LABEL}" \
  --agent "{AGENT}" \
  --model "{MODEL}" \
  {--mcp-config 'JSON' if MCP configured}
```

Validate:

```bash
codeprobe experiment validate {PATH}
```

---

## Phase 2: Execute

### Step 2a: Mine Tasks (if TASK_SOURCE=mine)

Delegate to `/mine-tasks` with experiment context:
- Target repo: `{REPO_URL}`
- Task count target: `{TASK_COUNT_TARGET}`
- Output directory: the experiment's tasks directory

After mining, re-validate:

```bash
codeprobe experiment validate {PATH}
```

### Step 2b: Pre-flight Validation

Source `.env.local` if present:

```bash
[ -f .env.local ] && source .env.local
```

For each configuration:
1. Verify agent CLI is available
2. If MCP configured, check the MCP server is reachable
3. Verify instruction variant files exist for each task

If anything fails, **stop and report** before running.

### Step 2c: Run Evaluations

Delegate to `/run-eval` with the experiment directory path. It will:
1. Loop over each configuration
2. Run all tasks with that config's settings
3. Write results per-config

Present progress as configs complete.

### Step 2d: Handle Interruptions

If interrupted, check status:

```bash
codeprobe experiment status {PATH}
```

Options: Resume, view partial results, or re-run a configuration.

---

## Phase 3: Interpret

Delegate to `/interpret` with the experiment directory. It will:
1. Compute statistical comparisons
2. Generate ranked leaderboard
3. Produce reports (interpretation.md, comparison-report.md, browse.html)

Or aggregate directly:

```bash
codeprobe experiment aggregate {PATH}
```

---

## Standalone Usage

Each phase can be run independently:

```bash
# Create experiment
codeprobe experiment init ./my-experiment --name "my-experiment"

# Add configurations
codeprobe experiment add-config ./my-experiment --label baseline --model claude-sonnet-4-6
codeprobe experiment add-config ./my-experiment --label with-mcp --model claude-sonnet-4-6 \
  --mcp-config '{"sourcegraph":{"command":"npx","args":["-y","@sourcegraph/mcp-server"]}}'

# Validate
codeprobe experiment validate ./my-experiment

# Check status
codeprobe experiment status ./my-experiment

# Aggregate results
codeprobe experiment aggregate ./my-experiment
```

---

## Quick Reference

| User says | What happens |
|-----------|-------------|
| `/experiment` | Full guided flow (Phase 0 -> 1 -> 2 -> 3) |
| "set up an experiment" | Same as `/experiment` |
| "compare baseline vs MCP" | Starts at Goal 1, pre-fills 2 configs |
| "compare Sonnet vs Opus" | Starts at Goal 2, pre-fills model configs |
| "test different prompts" | Starts at Goal 3 |
| "I have tasks, run them" | Starts at Goal 5, skips mining |
| "resume experiment X" | Loads experiment, checks status, resumes |
| "experiment status" | Runs `codeprobe experiment status` |
