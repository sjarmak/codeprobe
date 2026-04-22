---
name: scaffold
description: Create and validate eval task directories with the standard codeprobe layout. Scaffolds instruction.md, task.toml, and tests/test.sh for new tasks. Validates existing task directories for correctness. Triggers on scaffold task, create task, new task, validate task, task template.
user-invocable: false
---

# Scaffold -- Eval Task Creator & Validator

Create new eval task directories with the standard codeprobe layout, or validate existing ones. Each task gets instruction.md, task.toml, and tests/test.sh in a consistent structure.

Invokes `codeprobe scaffold` under the hood -- all operations run through the CLI, not Python imports.

---

## Subcommands

| Command             | Purpose                             |
| ------------------- | ----------------------------------- |
| `scaffold task`     | Create a new task directory         |
| `scaffold validate` | Validate an existing task directory |

---

## Phase 1: Determine Intent

**Question 1** -- Header: "What would you like to do?"

- Options:
  - **Create a new task** -- "Scaffold a fresh eval task directory"
  - **Validate an existing task** -- "Check an existing task directory for correctness"

---

## Phase 2a: Create Task

If creating, gather these inputs:

**Question 2** -- Header: "Task identity"

- "What is the task ID?" (e.g., `repo-bugfix-auth-001`)
- "Which repository does this target?" (e.g., `org/repo`)

**Question 3** -- Header: "Task details"

- "Describe the task instruction" (what the agent should do)
- "Short description" (one-line summary)

**Question 4** -- Header: "Task metadata"

- "Difficulty?" -- Options: **easy**, **medium** (default), **hard**
- "Category?" -- Options: **sdlc** (default), **probe_navigate**, **probe_comprehend**, **security**, **performance**
- "Time limit?" -- Options: **5 min** (300s, default), **2 min** (120s), **10 min** (600s), **Custom**
- "Reward type?" -- Options: **binary** (default), **test_ratio**, **exact_match**

Run the scaffold command:

```bash
codeprobe scaffold task \
  --id "{TASK_ID}" \
  --repo "{REPO}" \
  --output "{OUTPUT_DIR}" \
  --instruction "{INSTRUCTION}" \
  --description "{DESCRIPTION}" \
  --difficulty {DIFFICULTY} \
  --category {CATEGORY} \
  --time-limit {TIME_LIMIT_SEC} \
  --reward-type {REWARD_TYPE}
```

### Post-creation

Display the created directory structure:

```
Created task: {output_dir}/{task_id}/
  instruction.md     -- Task instruction for the agent
  task.toml          -- Task metadata (category, difficulty, reward)
  tests/
    test.sh          -- Scoring script (edit to add assertions)
```

Remind the user:

1. Edit `instruction.md` to refine the task description
2. Edit `tests/test.sh` to add scoring assertions
3. Optionally add `tests/ground_truth.json` for exact-match tasks

---

## Phase 2b: Validate Task

If validating:

**Question** -- Header: "Task directory"

- "Which task directory should I validate?"
- Default: current directory

Run validation:

```bash
codeprobe scaffold validate "{TASK_PATH}"
```

Display results:

- **Valid**: "Task directory is valid and ready for evaluation"
- **Errors**: List each error with severity (ERROR, WARNING) and message

---

## Task Directory Layout

Standard codeprobe task structure:

```
{task_id}/
  instruction.md      # What the agent should do (required)
  task.toml            # Metadata: category, difficulty, reward_type, time_limit_sec (required)
  tests/
    test.sh            # Scoring script, exit 0 = pass (required, must be executable)
    ground_truth.json  # Expected answer for exact_match tasks (optional)
```

### task.toml Format

```toml
task_id = "repo-bugfix-auth-001"
repo = "org/repo"
description = "Fix authentication bypass in login endpoint"
category = "sdlc"
difficulty = "medium"
reward_type = "binary"
time_limit_sec = 300
```

---

## Integration with Other Skills

- `/mine-tasks`: Mines real tasks from PR history -- use `/scaffold` when you need manual/custom tasks
- `/run-eval`: Run scaffolded tasks with `/run-eval {task_dir}`
- `/probe`: Generates probe tasks automatically -- `/scaffold` is for hand-crafted tasks

---

## Quick Reference

| User says                  | What happens                          |
| -------------------------- | ------------------------------------- |
| `/scaffold`                | Full guided flow (create or validate) |
| "create a new eval task"   | Scaffold task flow                    |
| "validate this task"       | Validate current directory            |
| "scaffold task for repo X" | Create with repo pre-filled           |
