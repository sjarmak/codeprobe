# Cross-Repo Workflow

Use this workflow when you need to mine tasks that span multiple repositories. This is common in microservice architectures, monorepo-adjacent setups, and when evaluating an agent's ability to use cross-repo tools like Sourcegraph MCP.

## Prerequisites

- Python 3.11+
- At least one agent installed (Claude Code, GitHub Copilot, or Codex)
- A primary repo with merge history
- One or more secondary repos (local paths or git URLs)
- A symbol-resolver backend — pick one (see [Backend selection](#backend-selection)):
  - **Sourcegraph** — most accurate; needs `codeprobe auth sourcegraph`
  - **AST** — tool-independent; needs the `go` toolchain on `PATH` for Go support
  - **Ripgrep** — coarse but always available

## Backend selection

`--backend` selects the symbol resolver used for cross-repo and `--mcp-families` ground truth.

| `--backend`     | Mechanism                            | Strengths                                  | Limitations                                   |
| --------------- | ------------------------------------ | ------------------------------------------ | --------------------------------------------- |
| `auto` (default) | Sourcegraph if authed, else ripgrep | Zero-config; matches prior behavior        | Falls back silently when SG auth is missing   |
| `sourcegraph`   | Sourcegraph `find_references` MCP    | Cross-repo type-aware resolution           | Network + auth required; tautology risk[^t]   |
| `ast`           | Local Python `ast` + Go `go/parser`  | Offline; tool-independent; no auth         | Intra-package only; no cross-package types    |
| `grep`          | `ripgrep --word-regexp`              | Always works; trivially deterministic      | No type or import context                     |

[^t]: When the agent under eval also uses the Sourcegraph MCP, ground truth derived from Sourcegraph is tautological — both sides resolve symbols through the same code-intel tool. Use `--backend ast` to break the tautology with a tool-independent oracle.

### When to choose `--backend ast`

The AST backend exists to end the `--mcp-families` tautology: Sourcegraph-derived ground truth and a Sourcegraph-MCP agent share the same resolver, so any disagreement is bias-free only when the oracle uses a different mechanism. Choose `ast` when:

- You're comparing MCP-enabled vs. MCP-disabled configs and want a fair oracle.
- You're running offline / airgapped and can't reach `sourcegraph.com`.
- The symbol you're targeting has unique enough naming that intra-package scoping is sufficient.

#### `ast` backend scope (v1)

In scope:

- **Python** (`.py`, `.pyi`): real `ast` walk. Resolves direct calls, method calls on local objects, definitions, and aliased imports. `mod.foo()` calls are filtered when `mod` is a known imported module — those go through the imported package, not a local method.
- **Go** (`.go`): real `go/parser` walk via an embedded helper invoked with `go run`. Resolves method declarations, method calls on local receivers, and bare function calls. `pkg.Symbol(...)` calls are filtered when `pkg` is in the file's import set.
- **Same-package scoping**: when constructed with `defining_file=`, results are restricted to the symbol's package directory — matches Sourcegraph's typed `find_references` semantics for intra-package callers.

Out of scope (deferred to v2):

- Cross-package Go type inference (interface satisfaction, full `gopls`-grade resolution).
- Macro-heavy languages (Rust, C++).
- Dynamic dispatch beyond Go interfaces and Python duck typing.
- Languages outside Python and Go (use `--backend sourcegraph` or `grep`).

Files with parse errors are skipped silently — they do not abort the scan.

## Step 1: Authenticate with Sourcegraph (one-time, if using `--backend sourcegraph`)

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
