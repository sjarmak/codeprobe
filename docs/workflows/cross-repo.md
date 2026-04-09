# Cross-Repo Workflow

Use this workflow when you need to mine tasks that span multiple repositories. This is common in microservice architectures, monorepo-adjacent setups, and when evaluating an agent's ability to use cross-repo tools like Sourcegraph MCP.

## Prerequisites

- Python 3.11+
- At least one agent installed (Claude Code, GitHub Copilot, or Codex)
- A primary repo with merge history
- One or more secondary repos (local paths or git URLs)
- Sourcegraph authentication (for cross-repo symbol resolution)

## Step 1: Authenticate with Sourcegraph (one-time)

Cross-repo mining uses Sourcegraph for symbol resolution across repos. Authenticate once and the token is cached.

```bash
codeprobe auth sourcegraph
```

**Expected output:**

```
Paste your Sourcegraph Personal Access Token: ****
Authenticated with https://sourcegraph.com
  Token cached at ~/.codeprobe/auth.json
```

To check auth status or remove cached credentials:

```bash
codeprobe auth status    # Show cached auth
codeprobe auth logout    # Clear cached token
```

## Step 2: Assess the primary repo

```bash
codeprobe assess /path/to/primary-repo
```

The primary repo should have rich merge history. See the [standard workflow](standard.md) for what a good assessment looks like.

## Step 3: Mine cross-repo tasks

Pass secondary repos with the `--cross-repo` flag. Each entry can be a local path or a git URL (which will be shallow-cloned automatically).

```bash
codeprobe mine /path/to/primary-repo \
  --cross-repo /path/to/repo-b \
  --cross-repo /path/to/repo-c \
  --goal mcp \
  --count 5 \
  --no-interactive
```

When `--cross-repo` is used without an explicit `--goal`, codeprobe defaults to `--goal mcp`.

**Expected output:**

```
Defaulting to --goal mcp for cross-repo mining
Mining tasks from /path/to/primary-repo ...
  Source: github (87 merged PRs)
  Goal: MCP / tool benefit
  Task type: mcp_tool_usage
  Count: 5
  Cross-repo: repo-b, repo-c

Using SourcegraphSymbolResolver for cross-repo mining

  [1/5] PR #42: Add shared auth token validation ... OK
  [2/5] PR #78: Update API contract between services ... OK
  ...
  [5/5] PR #15: Fix serialization mismatch across repos ... OK

5 tasks written to /path/to/primary-repo/.codeprobe/tasks/
Suite written to /path/to/primary-repo/.codeprobe/suite.toml
```

### Notes on cross-repo tasks

- MCP-goal tasks include an `instruction_mcp.md` variant alongside `instruction.md`. The MCP variant references available Sourcegraph tools (keyword_search, read_file, find_references, etc.). Use `--instruction-variant instruction_mcp.md` when running MCP configs to give agents tool-aware instructions.
- Each task's `metadata.json` includes a `ground_truth_commit` per repo, pinning the exact state used for ground truth generation.
- Tasks are written to the primary repo's `.codeprobe/tasks/` directory.
- The `--cross-repo` flag and `--org-scale` flag are mutually exclusive. Use `--cross-repo` for SDLC mining across repos; use `--org-scale` for org-scale comprehension mining within a single repo.
- Secondary repos can be specified as git URLs (e.g., `https://github.com/org/repo-b.git` or `org/repo-b` shorthand). They will be shallow-cloned to a temp directory.

## Step 4: Validate tasks

```bash
codeprobe validate /path/to/primary-repo/.codeprobe/tasks/<task-id>
```

Same validation as the standard workflow. Cross-repo tasks may use `dual` verification mode, which checks both `test.sh` and `ground_truth.json`.

**Expected output:**

```
  PASS  instruction.md exists (instruction.md present and non-empty)
  PASS  metadata parses (metadata.json parsed successfully)
  PASS  task_type valid (task_type 'mcp_tool_usage' is valid)
  PASS  verification_mode valid (verification_mode 'dual' is valid)
  PASS  tests/test.sh exists and executable (tests/test.sh present and executable)
  PASS  tests/ground_truth.json valid (ground_truth.json valid with answer_type)
```

## Step 5: Run agents

```bash
codeprobe run /path/to/primary-repo --agent claude --max-cost-usd 5.00
```

Cross-repo tasks tend to be harder and take longer. Consider using a higher cost budget and timeout.

```bash
codeprobe run /path/to/primary-repo \
  --agent claude \
  --max-cost-usd 10.00 \
  --timeout 600 \
  --parallel 3
```

## Step 6: Interpret results

```bash
codeprobe interpret /path/to/primary-repo
```

Same output format and export options as the standard workflow. See [standard.md](standard.md) for details.

## Full copy-pasteable sequence

```bash
# 0. Authenticate with Sourcegraph (one-time)
codeprobe auth sourcegraph

# 1. Assess
codeprobe assess /path/to/primary-repo

# 2. Mine cross-repo tasks
codeprobe mine /path/to/primary-repo \
  --cross-repo /path/to/repo-b \
  --cross-repo /path/to/repo-c \
  --goal mcp \
  --count 5 \
  --no-interactive

# 3. Validate (spot-check a task)
codeprobe validate /path/to/primary-repo/.codeprobe/tasks/<task-id>

# 4. Run agents
codeprobe run /path/to/primary-repo --agent claude --max-cost-usd 10.00 --timeout 600

# 5. Interpret
codeprobe interpret /path/to/primary-repo
```
