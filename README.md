# codeprobe

Benchmark AI coding agents against **your own codebase**.

Mine real tasks from your repo history, run agents against them, and find out which setup actually works best for **your** code, not someone else's benchmark suite.

## Why codeprobe?

Existing benchmarks (SWE-bench, HumanEval) use fixed task sets that AI models may have memorized from training data, and as general public benchmarks likely don't capture what is most important to your unique workflows. codeprobe mines tasks from **your private repo history**, producing benchmarks that are impossible to contaminate. You can also point the tool at any public repo to mine tasks from.

## Prerequisites

codeprobe orchestrates external AI coding agents — you need at least one installed:

| Agent              | Install                                          | Required env var                |
| ------------------ | ------------------------------------------------ | ------------------------------- |
| **Claude Code**    | [claude.ai/download](https://claude.ai/download) | `ANTHROPIC_API_KEY`             |
| **GitHub Copilot** | `npm install -g @github/copilot-cli` (>= 1.0.4)  | GitHub auth via `gh auth login` |
| **Codex**          | Included via `pip install codeprobe[codex]`      | `OPENAI_API_KEY`                |

You also need:

- **Python 3.11+**
- **Git** (for task mining and worktree isolation)
- **GitHub CLI** (`gh`) — optional, for mining tasks from GitHub PRs with linked issues

The `assess` and `mine --enrich` commands need an LLM for scoring/enrichment. codeprobe auto-detects the best available backend:

| Priority | Backend       | Install                                          | Env var             |
| -------- | ------------- | ------------------------------------------------ | ------------------- |
| 1        | Anthropic SDK | `pip install codeprobe[anthropic]`               | `ANTHROPIC_API_KEY` |
| 2        | OpenAI SDK    | `pip install codeprobe[codex]`                   | `OPENAI_API_KEY`    |
| 3        | Claude CLI    | [claude.ai/download](https://claude.ai/download) | `ANTHROPIC_API_KEY` |

Override with `CODEPROBE_LLM_BACKEND=anthropic|openai|claude-cli`. Without any backend, `assess` falls back to heuristic scoring.

## Quick Start

```bash
pip install codeprobe

cd /path/to/your/repo

codeprobe assess .      # Score benchmarking potential (optional)
codeprobe mine .        # Extract tasks from repo history
codeprobe run .         # Run agents against tasks
codeprobe interpret .   # Get recommendations
```

Prefer driving codeprobe through a coding agent instead? See [docs/workflows/with-agents.md](docs/workflows/with-agents.md) for the skills-based workflow (`/experiment`, `/assess-codebase`, `/interpret`).

## Commands

| Command                    | Purpose                                          |
| -------------------------- | ------------------------------------------------ |
| `codeprobe assess`         | Score a codebase's benchmarking potential        |
| `codeprobe init`           | Interactive wizard — choose what to compare      |
| `codeprobe mine`           | Mine eval tasks from merged PRs/MRs              |
| `codeprobe probe`          | Generate fast micro-benchmark probes (30s each)  |
| `codeprobe experiment`     | Manage comparison experiments (init, add-config) |
| `codeprobe run`            | Execute tasks against AI agents                  |
| `codeprobe interpret`      | Analyze results, rank configurations             |
| `codeprobe doctor`         | Check environment readiness (agents, keys, git)  |
| `codeprobe preambles list` | List available preambles at all search levels    |
| `codeprobe oracle-check`   | Compare agent answer against oracle ground truth |
| `codeprobe scaffold`       | Create/validate eval task directories            |
| `codeprobe ratings`        | Record and analyze agent session quality ratings |

## Two Ways to Generate Tasks

### 1. SDLC Tasks (from merged PRs)

Mine real code-change tasks from your git history. Agents must reproduce known fixes and features.

```bash
codeprobe mine . --count 10 --source github
codeprobe mine . --count 5 --min-files 4    # Harder tasks (more files changed)
codeprobe mine . --enrich                    # LLM-enriched instructions
```

### 2. Micro-Benchmark Probes

Fast exact-match tasks (30s each) that test code navigation and comprehension — no agent sandbox needed.

```bash
codeprobe probe . -n 10 -l python -s 42 -o ./probes
```

Generates four probe types: find-function, count-callers, return-type, module-dependency.

## Curation Workflows

End-to-end flows from a raw repo to ranked agent results. Each workflow covers the full `assess → mine → validate → run → interpret` pipeline.

| Workflow       | When to use                               | Guide                                                        |
| -------------- | ----------------------------------------- | ------------------------------------------------------------ |
| **Standard**   | Repo has merged PRs/MRs                   | [docs/workflows/standard.md](docs/workflows/standard.md)     |
| **Cold-start** | New repo, squashed history, vendored code | [docs/workflows/cold-start.md](docs/workflows/cold-start.md) |
| **Cross-repo** | Tasks spanning multiple repositories      | [docs/workflows/cross-repo.md](docs/workflows/cross-repo.md) |

**Quick start (standard path):**

```bash
codeprobe assess /path/to/repo
codeprobe mine /path/to/repo --goal quality --count 10 --no-interactive
codeprobe validate /path/to/repo/.codeprobe/tasks/<task-id>
codeprobe run /path/to/repo --agent claude --max-cost-usd 5.00
codeprobe interpret /path/to/repo
```

For the full MCP comparison setup (preambles, baseline vs with-MCP configs), see the next section.

## MCP Comparison Experiments

Compare agent performance with and without MCP tools (Sourcegraph, GitHub, etc.).

### Mine org-scale comprehension tasks

```bash
# Set up Sourcegraph credentials
export SOURCEGRAPH_TOKEN="your-token"

# Mine MCP-optimized tasks with Sourcegraph ground truth enrichment
codeprobe mine /path/to/repo \
  --org-scale --mcp-families --count 5 \
  --no-interactive --no-llm \
  --sg-repo github.com/sg-evals/your-repo
```

MCP task families: `symbol-reference-trace`, `type-hierarchy-consumers`, `change-scope-audit`.

### Set up the experiment

```bash
# Create experiment
codeprobe experiment init /path/to/repo --name mcp-comparison

# Copy mined tasks into the experiment
cp -r /path/to/repo/.codeprobe/tasks/* /path/to/repo/mcp-comparison/tasks/

# Baseline config (no MCP, no preamble)
codeprobe experiment add-config /path/to/repo/mcp-comparison \
  --label baseline --agent claude --model claude-haiku-4-5-20251001

# Sourcegraph MCP config (preamble + MCP server)
codeprobe experiment add-config /path/to/repo/mcp-comparison \
  --label with-sourcegraph --agent claude --model claude-haiku-4-5-20251001 \
  --preamble sourcegraph \
  --mcp-config '{"mcpServers":{"sourcegraph":{"type":"http","url":"https://sourcegraph.com/.api/mcp/v1","headers":{"Authorization":"token ${SOURCEGRAPH_TOKEN}"}}}}'

# Run and interpret
codeprobe run /path/to/repo/mcp-comparison --agent claude --max-cost-usd 5.00
codeprobe interpret /path/to/repo/mcp-comparison
```

### Preambles

Preambles are composable instruction templates prepended to the agent's prompt for MCP-enabled configs. Built-in preambles: `sourcegraph`, `github`.

Override built-ins by placing a `.md` file in:

- `<task_dir>/preambles/` (per-task)
- `.codeprobe/preambles/` (project-level)
- `~/.codeprobe/preambles/` (user-level)

Template variables: `{{sg_repo}}`, `{{repo_name}}`, `{{repo_path}}`, `{{task_id}}`

## Key Flags

```bash
# Running
codeprobe run . --parallel 5          # Run 5 tasks concurrently (worktree-isolated)
codeprobe run . --max-cost-usd 2.00   # Stop when cost budget is reached
codeprobe run . --dry-run             # Estimate resource usage without running
codeprobe run . --model opus-4        # Override experiment.json model
codeprobe run . --timeout 600         # Override default 300s timeout
codeprobe run . --repeats 3           # Run each task 3 times
codeprobe run . --show-prompt         # Print resolved prompt without running agent

# Mining
codeprobe mine . --enrich             # Use LLM to improve weak task instructions
codeprobe mine . --org-scale          # Mine comprehension tasks (not SDLC)
codeprobe mine . --mcp-families       # Include MCP-optimized task families
codeprobe mine . --sg-repo REPO       # Sourcegraph repo for ground truth enrichment
codeprobe mine . --preset quick       # Quick scan: count=3
codeprobe mine . --preset mcp         # MCP eval: org-scale + MCP families + enrich

# Mine profiles (save/load custom flag combinations)
codeprobe mine --save-profile my-setup --count 10 --org-scale .
codeprobe mine --profile my-setup .   # Load saved flags
codeprobe mine --list-profiles        # Show available profiles

# Experiment configs
codeprobe experiment add-config . --preamble sourcegraph  # Attach MCP preamble
codeprobe experiment add-config . --mcp-config config.json  # Attach MCP server

# Diagnostics
codeprobe doctor                      # Check agents, API keys, git, Python
codeprobe preambles list              # Show available preambles at all levels

# Output
codeprobe interpret . --format csv    # Export for pivot tables
codeprobe interpret . --format html   # Self-contained HTML report
```

## Supported Agents

- **Claude Code** (`--agent claude`) — headless via `claude -p`
- **GitHub Copilot** (`--agent copilot`) — via Copilot CLI
- **Codex** (`--agent codex`) — via OpenAI API
- Custom agents via the `AgentAdapter` protocol

## Supported Git Hosts

GitHub, GitLab, Bitbucket, Azure DevOps, Gitea/Forgejo, and local repos.

## Configuration

Configuration lives in `experiment.json` (created by `codeprobe init` or `codeprobe experiment init`). CLI flags override experiment.json values — precedence: built-in defaults < experiment.json < CLI flags.

Run-time observability is on by default: Rich Live dashboard in TTY, JSON event lines with `--log-format json` for CI. Cost budget warnings at 80% and 100% thresholds are always visible on stderr.

## License

Apache-2.0
