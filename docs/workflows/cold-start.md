# Cold-Start Workflow

Use this workflow when the target repo lacks rich merge history: new repos, monorepos with squashed history, vendored/third-party code, or repos where `codeprobe assess` reports a low score.

The key difference from the [standard workflow](standard.md) is how tasks are generated. Instead of mining from merged PRs, you generate synthetic comprehension tasks from the current repo state.

## Prerequisites

- Python 3.11+
- At least one agent installed (Claude Code, GitHub Copilot, or Codex)
- The target repo exists locally (no merge history required)

## Step 1: Assess the repo

Run assess to confirm that SDLC mining is not viable and to see what the tool recommends.

```bash
codeprobe assess /path/to/repo
```

**Expected output (cold-start candidate):**

```
Codebase Assessment: new-project
==================================================

Scoring method: model (claude-sonnet-4-20250514)
Overall Score: 28%

Breakdown:
  merge_history           5%  3 merged PRs, insufficient for SDLC mining
  test_coverage          40%  Basic test suite, 22% line coverage
  complexity             55%  Moderate complexity across 12 modules
  documentation          20%  Minimal documentation

Recommendation: Low merge history — consider codebase navigation tasks
or micro-benchmark probes instead of SDLC mining.

Next: codeprobe mine . --count 5
```

A low `merge_history` score (below ~30%) is the signal to use this workflow.

## Step 2: Generate tasks

You have two options for generating tasks without merge history. Both produce task directories in the standard layout that `codeprobe run` can execute.

### Option A: Mine navigation tasks

Navigation tasks test architecture comprehension. They are generated from static analysis of the current repo state and do not require merge history.

```bash
codeprobe mine /path/to/repo --goal navigation --count 10 --no-interactive
```

**Expected output:**

```
Mining tasks from /path/to/repo ...
  Source: local (static analysis)
  Goal: Codebase navigation
  Task type: architecture_comprehension
  Count: 10

  [1/10] Trace call chain: main() → process_request() ... OK
  [2/10] Dependency analysis: which modules change if auth is replaced? ... OK
  [3/10] Return type resolution: Config.get_value() at line 42 ... OK
  ...
  [10/10] Transitive dependency: does models depend on utils? ... OK

10 tasks written to /path/to/repo/.codeprobe/tasks/
Suite written to /path/to/repo/.codeprobe/suite.toml
```

### Option B: Generate micro-benchmark probes

Probes are fast exact-match tasks (about 30s each) that test code navigation and comprehension. They need no git history at all.

```bash
codeprobe probe /path/to/repo -n 10 -l python -s 42 -o /path/to/repo/probes --emit-tasks
```

Flags:

| Flag           | Purpose                                    |
| -------------- | ------------------------------------------ |
| `-n 10`        | Number of probes to generate               |
| `-l python`    | Language to target                         |
| `-s 42`        | Random seed for reproducibility            |
| `-o <dir>`     | Output directory                           |
| `--emit-tasks` | Write probes as task directories for `run` |

Probe types generated: `find-function`, `count-callers`, `return-type`, `module-dependency`.

## Step 3: Validate tasks

Same as the standard workflow. Spot-check a few tasks.

```bash
codeprobe validate /path/to/repo/.codeprobe/tasks/<task-id>
```

**Expected output:**

```
  PASS  instruction.md exists (instruction.md present and non-empty)
  PASS  metadata parses (metadata.json parsed successfully)
  PASS  task_type valid (task_type 'architecture_comprehension' is valid)
  PASS  verification_mode valid (verification_mode 'artifact_eval' is valid)
  PASS  tests/test.sh exists and executable (tests/test.sh present and executable)
  PASS  tests/ground_truth.json valid (ground_truth.json valid with answer_type)
```

## Step 4: Run agents

Identical to the standard workflow. Probes are cheaper to run since they typically complete in under a minute.

```bash
codeprobe run /path/to/repo --agent claude --max-cost-usd 2.00
```

**Expected output:**

```
Running config: default (10 tasks)
  trace-call-chain-1: PASS (28.4s)
  dep-analysis-auth-2: FAIL (45.1s)
  return-type-config-3: PASS (18.7s)
  ...
  default: 6/10 passed

Finished: 10/10 tasks, mean score 0.60, total cost $0.62

Next: codeprobe interpret .
```

## Step 5: Interpret results

```bash
codeprobe interpret /path/to/repo
```

Same output format and export options as the standard workflow. See [standard.md](standard.md) for details.

## Full copy-pasteable sequence

### Using navigation tasks

```bash
# 1. Assess (confirms cold-start scenario)
codeprobe assess /path/to/repo

# 2. Mine navigation tasks
codeprobe mine /path/to/repo --goal navigation --count 10 --no-interactive

# 3. Validate (spot-check a task)
codeprobe validate /path/to/repo/.codeprobe/tasks/<task-id>

# 4. Run agents
codeprobe run /path/to/repo --agent claude --max-cost-usd 2.00

# 5. Interpret
codeprobe interpret /path/to/repo
```

### Using micro-benchmark probes

```bash
# 1. Assess
codeprobe assess /path/to/repo

# 2. Generate probes as task directories
codeprobe probe /path/to/repo -n 10 -l python -s 42 -o /path/to/repo/probes --emit-tasks

# 3. Validate
codeprobe validate /path/to/repo/probes/<task-id>

# 4. Run agents
codeprobe run /path/to/repo --agent claude --max-cost-usd 2.00

# 5. Interpret
codeprobe interpret /path/to/repo
```
