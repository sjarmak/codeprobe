# Standard Workflow

Use this workflow when the target repo has merged PRs or MRs that codeprobe can mine ground truth from. This is the most common path.

## Prerequisites

- Python 3.11+
- At least one agent installed (Claude Code, GitHub Copilot, or Codex)
- The target repo has merge history (merged PRs/MRs)

## Step 1: Assess the repo

Score the repo's benchmarking potential. This is optional but recommended as a sanity check before mining.

```bash
codeprobe assess /path/to/repo
```

**Expected output:**

```
Codebase Assessment: my-project
==================================================

Scoring method: model (claude-sonnet-4-20250514)
Overall Score: 78%

Breakdown:
  merge_history          85%  142 merged PRs in the last 6 months
  test_coverage          72%  pytest suite with 68% line coverage
  complexity             80%  Moderate cyclomatic complexity across 34 modules
  documentation          65%  README and partial docstrings

Recommendation: Good candidate for benchmarking. Rich merge history supports SDLC task mining.

Next: codeprobe mine . --count 5
```

If the overall score is below 50%, consider the [cold-start workflow](cold-start.md) instead.

## Step 2: Mine tasks

Extract tasks for the eval goal you care about. The `--goal` flag selects the task type and applies sensible defaults.

```bash
codeprobe mine /path/to/repo --goal quality --count 10 --no-interactive
```

Available goals:

| Goal         | Task type                    | Description                                   |
| ------------ | ---------------------------- | --------------------------------------------- |
| `quality`    | `sdlc_code_change`           | Compare code-change quality across agents     |
| `navigation` | `architecture_comprehension` | Test codebase navigation and understanding    |
| `mcp`        | `mcp_tool_usage`             | Harder cross-file tasks benefiting from tools |
| `general`    | Balanced mix                 | Default if `--goal` is omitted                |

**Expected output:**

```
Mining tasks from /path/to/repo ...
  Source: github (142 merged PRs)
  Goal: Code quality comparison
  Task type: sdlc_code_change
  Count: 10

  [1/10] PR #87: Fix race condition in cache invalidation ... OK
  [2/10] PR #134: Add retry logic to API client ... OK
  [3/10] PR #91: Refactor auth middleware ... OK
  ...
  [10/10] PR #45: Update serialization for nested models ... OK

10 tasks written to /path/to/repo/.codeprobe/tasks/
Suite written to /path/to/repo/.codeprobe/suite.toml

Next: codeprobe validate /path/to/repo/.codeprobe/tasks/<task-id>
```

Each task directory contains:

```
.codeprobe/tasks/<task-id>/
  instruction.md       # What the agent must do
  instruction_mcp.md   # MCP-augmented variant (only for --goal mcp tasks)
  metadata.json        # Metadata (task_type, difficulty, verification_mode)
  tests/
    test.sh            # Automated verifier (exit 0 = pass)
    ground_truth.json  # Expected answer (for artifact tasks)
```

## Step 3: Validate tasks

Check that each mined task is structurally correct before running agents against it.

```bash
codeprobe validate /path/to/repo/.codeprobe/tasks/<task-id>
```

**Expected output (passing):**

```
  PASS  instruction.md exists (instruction.md present and non-empty)
  PASS  metadata parses (metadata.json parsed successfully)
  PASS  task_type valid (task_type 'sdlc_code_change' is valid)
  PASS  verification_mode valid (verification_mode 'test_script' is valid)
  PASS  tests/test.sh exists and executable (tests/test.sh present and executable)
```

**Expected output (failing):**

```
  PASS  instruction.md exists (instruction.md present and non-empty)
  PASS  metadata parses (metadata.json parsed successfully)
  FAIL  tests/test.sh exists (tests/test.sh not found)
```

Add `--strict` for an LLM spot-check of ground truth plausibility (experimental).

## Step 4: Run agents

Execute the tasks against one or more agents. codeprobe isolates each run in a git worktree so agents cannot interfere with each other.

```bash
codeprobe run /path/to/repo --agent claude --max-cost-usd 5.00
```

**Expected output:**

```
Running config: default (10 tasks)
  fix-cache-race-87: PASS (42.3s)
  add-retry-api-134: FAIL (67.1s)
  refactor-auth-91: PASS (38.9s)
  ...
  default: 7/10 passed

Finished: 10/10 tasks, mean score 0.70, total cost $1.84

Next: codeprobe interpret .
```

Useful flags:

| Flag               | Purpose                                |
| ------------------ | -------------------------------------- |
| `--parallel 5`     | Run 5 tasks concurrently               |
| `--max-cost-usd 2` | Stop when cost budget is reached       |
| `--dry-run`        | Estimate cost and disk usage           |
| `--model opus-4`   | Override the model for this run        |
| `--timeout 600`    | Override the default 300s task timeout |
| `--repeats 3`      | Run each task 3 times for consistency  |

## Step 5: Interpret results

Analyze the run results and get actionable recommendations.

```bash
codeprobe interpret /path/to/repo
```

**Expected output:**

```
Experiment: my-project
==================================================

Config: default (claude, claude-sonnet-4-20250514)
  Tasks:  10
  Passed: 7 (70%)
  Mean score: 0.70
  Total cost: $1.84
  Avg time:   45.2s

Recommendation: Strong baseline. Consider running with --parallel 5 and
comparing against a second config (e.g., with MCP tools enabled).
```

Export formats:

```bash
codeprobe interpret /path/to/repo --format csv   # For pivot tables
codeprobe interpret /path/to/repo --format html  # Self-contained HTML report
codeprobe interpret /path/to/repo --format json  # Machine-readable
```

## Full copy-pasteable sequence

```bash
# 1. Assess
codeprobe assess /path/to/repo

# 2. Mine tasks
codeprobe mine /path/to/repo --goal quality --count 10 --no-interactive

# 3. Validate (spot-check a task)
codeprobe validate /path/to/repo/.codeprobe/tasks/<task-id>

# 4. Run agents
codeprobe run /path/to/repo --agent claude --max-cost-usd 5.00

# 5. Interpret
codeprobe interpret /path/to/repo
```
