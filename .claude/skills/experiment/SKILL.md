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

| #   | Option                                       | Description                                                                                                                                  |
| --- | -------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | **Does an MCP tool help my agent?**          | Compare your agent with and without an MCP server (like Sourcegraph). See if MCP-backed code search makes the agent faster or more accurate. |
| 2   | **Which model works best for my codebase?**  | Run the same tasks on different models (e.g., Sonnet vs Opus) to see which handles your code best.                                           |
| 3   | **How do different prompts affect results?** | Test different instruction styles or system prompts to find what gets the best agent behavior.                                               |
| 4   | **Custom comparison**                        | Full control -- define exactly what varies between configurations.                                                                           |
| 5   | **I already have tasks, just run them**      | Skip task mining. Point to existing tasks and set up configurations.                                                                         |

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

| #   | Option                                   | Description                                                                                       |
| --- | ---------------------------------------- | ------------------------------------------------------------------------------------------------- |
| 1   | **Mine from a repo**                     | Point at a repo and automatically extract real coding tasks from merged PRs. Takes ~5-10 minutes. |
| 2   | **Use existing tasks in this directory** | I already have task directories (with `instruction.md` and `task.toml` files).                    |
| 3   | **Point me to a task directory**         | I'll specify the path.                                                                            |

If **Mine from a repo**: record `TASK_SOURCE=mine`. Will delegate to `/mine-tasks` in Phase 2.
If **Use existing tasks**: scan current directory for task directories (contain `instruction.md` or `task.toml`). List what was found. Record `TASK_SOURCE=existing`.
If **Point me to a directory**: ask for path. Record `TASK_SOURCE=path`.

**If Goal is 5** (existing tasks): Go directly to the scan/path flow above.

### Step 0d: Task Count Guidance (only if TASK_SOURCE=mine)

**Question** -- Header: "How thorough should the evaluation be?"

| #   | Option         | Tasks | Description                                                                         |
| --- | -------------- | ----- | ----------------------------------------------------------------------------------- |
| 1   | **Quick look** | 3-5   | Fast results. Good for a first experiment or validating your setup works.           |
| 2   | **Standard**   | 5-10  | Good balance of coverage and speed. Enough tasks to see patterns.                   |
| 3   | **Thorough**   | 10-20 | More statistical confidence in the results. Best for making real tooling decisions. |

Record as `TASK_COUNT_TARGET`.

### Step 0e: Task Type Distribution (only if TASK_SOURCE=mine)

**Question** -- Header: "What kinds of tasks should the eval cover?"

By default the eval mines a single task family chosen from your goal in Step 0a (e.g. Goal 1 → MCP-style symbol/type/scope tasks). For honest comparisons across task styles — especially when you want to know "does MCP help on SDLC tasks the same way it helps on symbol-reference-trace?" — you can specify a mix.

| #   | Option                          | Description                                                                           |
| --- | ------------------------------- | ------------------------------------------------------------------------------------- |
| 1   | **Auto (based on goal)**        | Default. Use the family bound to your Step 0a goal. Backward-compatible.              |
| 2   | **Custom task type mix**        | Specify counts per task type (e.g. 5 symbol-reference-trace + 5 SDLC + 5 oracle_checks). |

**If Auto:** Record `TASK_DISTRIBUTION="auto"`. Mining proceeds with the goal-bound default.

**If Custom mix:** Show the user the **Available Task Types** table (see reference at the end of this skill). Ask:

- For each type they want, how many tasks (must sum to ≤ `TASK_COUNT_TARGET`).
- Validate the sum.

Record as `TASK_DISTRIBUTION` (a JSON object), e.g.:

```json
{
  "symbol-reference-trace": 5,
  "sdlc": 5,
  "oracle_checks": 5
}
```

> **Mining support note:** Not every task family currently has a dedicated miner. See the **Available Task Types** table for which types are wired vs. which surface as follow-ups (`MINING_HARDCODED_FOR=<list>` is reported back to the user after mining).

---

## Phase 1: Configure Comparisons

Build the configuration matrix. The questions depend on `EXPERIMENT_GOAL`.

### Step 1a: Pre-fill from Goal

**Goal 1 (MCP comparison):**
Pre-create 2 configurations:

| Config | Label      | Agent  | Model  | MCP        | Preamble            |
| ------ | ---------- | ------ | ------ | ---------- | ------------------- |
| A      | `baseline` | (same) | (same) | none       | none                |
| B      | `with-mcp` | (same) | (same) | (provider) | (provider preamble) |

Then ask:

- **Question**: "Which agent?" -- Claude (default) or Copilot
- **Question**: "Which model?" -- Default: `claude-sonnet-4-6`
- **Question**: "Which MCP provider?" -- Sourcegraph / GitHub / Custom / I'll configure later

**If Sourcegraph:**

- Source `.env.local` to get `SOURCEGRAPH_ACCESS_TOKEN` and `SOURCEGRAPH_URL`
- Build MCP config JSON: `{"mcpServers":{"sourcegraph":{"type":"http","url":"{SG_URL}/.api/mcp/all","headers":{"Authorization":"token {SG_TOKEN}"}}}}`
- Use `--preamble sourcegraph` (built-in v2 preamble; `task_preamble_context` fills `{{repo_scope}}` from `metadata.sg_repo` and `{{workflow_tail}}` per `metadata.category`)
- Mine tasks with `--org-scale --mcp-families --sg-repo {SG_REPO}` (see `/mine-tasks` MCP flow)
- For oracle / symbol-reference-trace / change-scope-audit tasks
  (text answers, not code edits): also pass `--hide-local-source` to
  stash the workspace source for the duration of the run. Forces
  MCP-only access, mirrors CSB `Dockerfile.sg_only` / EB
  `generate_sg_only_dockerfile`. **Skip** for SDLC tasks — they need
  files to edit.

**If GitHub:**

- Use `--preamble github` (built-in preamble)
- User provides MCP config JSON for their GitHub MCP server

**Preamble override:** Users can customize the built-in preamble by placing a `sourcegraph.md` or `github.md` in `.codeprobe/preambles/` at the task, project, or user level. The built-in is the fallback.

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
- **Task distribution:** `{TASK_DISTRIBUTION}` (from Step 0e — `"auto"` or a JSON object of `{family: count}`). When `auto`, mine-tasks falls back to its goal-bound default (e.g. `--mcp-families` for Goal 1). When a custom mix is supplied, mine-tasks honors per-family counts where supported and reports `MINING_HARDCODED_FOR=<list>` for any unsupported families.
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

**Cost-cap note (v0.10.0+):** `codeprobe run` defaults to
`--config-parallel 1` (configs run serially). This keeps
`--max-cost-usd` honest — cross-config parallelism multiplies in-flight
task count and inflates cost-cap overshoot proportionally. Pass
`--config-parallel N` only when you want to trade cost-cap precision
for wall-clock speed.

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

# Add baseline (no MCP, no preamble)
codeprobe experiment add-config ./my-experiment --label baseline --model claude-sonnet-4-6

# Add Sourcegraph MCP config (preamble + MCP server)
codeprobe experiment add-config ./my-experiment --label with-sourcegraph \
  --model claude-sonnet-4-6 \
  --preamble sourcegraph \
  --mcp-config '{"mcpServers":{"sourcegraph":{"type":"http","url":"https://sourcegraph.com/.api/mcp/all","headers":{"Authorization":"token ${SOURCEGRAPH_TOKEN}"}}}}'

# Add GitHub MCP config (different preamble + different MCP server)
codeprobe experiment add-config ./my-experiment --label with-github \
  --model claude-sonnet-4-6 \
  --preamble github \
  --mcp-config github-mcp.json

# Validate
codeprobe experiment validate ./my-experiment

# Check status
codeprobe experiment status ./my-experiment

# Aggregate results
codeprobe experiment aggregate ./my-experiment
```

### Preamble System

Built-in preambles: `sourcegraph`, `github`. Override by placing a `.md` file in:

- `<task_dir>/preambles/` (per-task)
- `.codeprobe/preambles/` (project-level)
- `~/.codeprobe/preambles/` (user-level)

Template variables (filled by `task_preamble_context` at compose time):

- `{{sg_repo}}`, `{{repo_name}}`, `{{repo_path}}`, `{{task_id}}` — task identity
- `{{repo_scope}}` — one-line repo-scoping directive (sourcegraph
  preamble v2; built from `metadata.sg_repo`)
- `{{workflow_tail}}` — category-specialised continuation of the
  numbered "Required Workflow" list (sourcegraph preamble v2; varies
  by `metadata.category`: oracle_checks, symbol-reference-trace,
  sdlc, or default)

Pair the v2 sourcegraph preamble with `--hide-local-source` for a
true sg-only comparison: workspace source is stashed for the run so
the agent has nothing local to fall back on. Compatible with text-
answer task families (oracle_checks, symbol-reference-trace,
change-scope-audit). Not compatible with SDLC (which needs source
to edit).

---

## Available Task Types

The eval framework supports the following task families. Each maps to a `scorer_family` from `codeprobe.core.scoring.SCORER_FAMILIES`. When you choose a Custom task type mix in Step 0e, pick one or more from this table.

| Task type                    | scorer_family            | What it tests                                                              | Mining support             |
| ---------------------------- | ------------------------ | -------------------------------------------------------------------------- | -------------------------- |
| `symbol-reference-trace`     | `oracle_overlap_f1`      | Find all files referencing a symbol (catches aliases, re-exports)          | Wired (`--mcp-families`)   |
| `type-hierarchy-consumers`   | `oracle_overlap_f1`      | Find implementations and consumers of base classes                         | Wired (`--mcp-families`)   |
| `change-scope-audit`         | `oracle_overlap_f1`      | Blast radius: all files affected by changing a symbol                      | Wired (`--mcp-families`)   |
| `mcp-fbeta`                  | `oracle_overlap_fbeta`   | Same as above with per-task β (precision-aware MCP comparison)             | Wired (`--mcp-families` + `verification.fbeta_beta`) |
| `org-scale-tier-weighted`    | `oracle_weighted_f1`     | Cross-cutting org-scale tasks with tiered weights (header / impl / test)   | Wired (`--org-scale`)       |
| `org-scale-recall-tilted`    | `oracle_weighted_recall` | Same as above, recall-leaning for triage / discovery                       | Wired (`--org-scale`)       |
| `dependency-chain`           | `sequence_lcs`           | Order-sensitive dependency chains (call-graph traversals)                  | Wired (general SDLC mining) |
| `sdlc`                       | `weighted_checkpoints`   | SDLC-style: build / test / refactor / doc tasks with per-checkpoint scoring | Wired (general SDLC mining) |
| `oracle_checks` (CSB-org-style) | `oracle_checks`       | Structured-rubric criteria: per-criterion pass/fail with weights           | **Hardcoded for follow-up** — port from CSB pending |
| `binary-test`                | `binary_test`            | Single test.sh exit-code tasks                                             | Wired (general SDLC mining) |
| `continuous`                 | `continuous`             | Reward.txt or stdout-float scoring                                         | Wired (general SDLC mining) |
| `exact-match`                | `exact_match`            | Count, boolean, or text equality                                           | Wired (general SDLC mining) |
| `dual-composite`             | `dual_composite`         | Composite of direct + artifact scoring                                     | Wired (general SDLC mining) |

> **Backward compatibility:** if Step 0e is set to `auto`, the eval uses whatever family the chosen Goal would have used previously (Goal 1 → MCP families; Goals 2/3/4 → SDLC mix). No existing eval invocation breaks.

> **Distribution honored vs hardcoded:** mining honors `TASK_DISTRIBUTION` for families marked **Wired** above. For any family marked **Hardcoded for follow-up**, the experiment skill will surface the gap as `MINING_HARDCODED_FOR=<list>` after mining, and the eval will run on whatever subset the miner could produce. Currently `oracle_checks` mining is the open follow-up — track in the codeprobe rig as a successor to `bln9`.

---

## Quick Reference

| User says                 | What happens                              |
| ------------------------- | ----------------------------------------- |
| `/experiment`             | Full guided flow (Phase 0 -> 1 -> 2 -> 3) |
| "set up an experiment"    | Same as `/experiment`                     |
| "compare baseline vs MCP" | Starts at Goal 1, pre-fills 2 configs     |
| "compare Sonnet vs Opus"  | Starts at Goal 2, pre-fills model configs |
| "test different prompts"  | Starts at Goal 3                          |
| "I have tasks, run them"  | Starts at Goal 5, skips mining            |
| "mix task types"          | Branches to Step 0e Custom mix            |
| "resume experiment X"     | Loads experiment, checks status, resumes  |
| "experiment status"       | Runs `codeprobe experiment status`        |
