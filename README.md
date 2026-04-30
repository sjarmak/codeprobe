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

### Avoid the ground-truth tautology (read first)

When `--mcp-families` mining writes ground truth using a single backend (e.g. Sourcegraph's `sg_find_references`), and the experiment then gives one config the *same* MCP tool, the with-MCP config can score 1.0 simply because it called the backend that wrote the answer key. The reported delta then measures "did the agent invoke the grading rubric" rather than tool value (tracked as `codeprobe-ekhi`).

codeprobe ships three structural mitigations that are on by default; do not disable them unless you know what you are giving up:

1. **Multi-source consensus mining** — `--mcp-families` runs every available backend (`sourcegraph`, `ast`, `grep`) and only ships tasks where ≥2 backends agree above `--consensus-threshold` (default `0.8` pairwise file-level F1). Tasks below the threshold are quarantined under `tasks_quarantined/` with a `divergence_report.json`. `--consensus-mode intersection` (default) keeps the high-precision intersection; `--consensus-mode union` keeps everything any backend found. `--no-consensus` reverts to legacy single-backend GT and is unsafe for MCP-vs-no-MCP comparisons.
2. **Tool-independent AST oracle** — `--backend ast` (also one of the consensus backends) resolves ground truth via Python `ast` and a Go scanner, with no dependency on Sourcegraph or grep. Use it as a standalone backend or as the independent leg of consensus.
3. **Aggregate-time bias detection** — `codeprobe experiment aggregate` flags `backend_overlap`, `overshipping`, and `no_independent_baseline` patterns before printing the score table. See [How to read aggregate output](#how-to-read-aggregate-output).

After mining, also run cross-validation to surface remaining divergences across the per-backend ground-truth files:

```bash
codeprobe mine-cross-validate /path/to/repo/.codeprobe/tasks \
  --threshold 0.6   # exit 1 if any pair falls below — useful in CI
```

The full command set above is the supported path for honest MCP-vs-no-MCP measurement; tasks that survive consensus + cross-validation are independent of the agent's tool surface and safe to publish.

### Mine org-scale comprehension tasks

```bash
# Set up Sourcegraph credentials (used as one of the consensus backends)
export SOURCEGRAPH_TOKEN="your-token"

# Mine MCP-optimized tasks with default consensus across sourcegraph + ast + grep
codeprobe mine /path/to/repo \
  --org-scale --mcp-families --count 5 \
  --no-interactive --no-llm \
  --sg-repo github.com/sg-evals/your-repo

# Optional: cross-validate the resulting per-backend ground truths
codeprobe mine-cross-validate /path/to/repo/.codeprobe/tasks
```

MCP task families: `symbol-reference-trace`, `type-hierarchy-consumers`, `change-scope-audit`.

> The Sourcegraph token enables the SG leg of consensus. With no token, consensus falls back to `ast + grep`; you'll see fewer shipped tasks but the comparison stays honest. Pass `--backend ast` to skip Sourcegraph entirely.

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
codeprobe mine . --backend ast        # Tool-independent ground truth (Python + Go AST)
codeprobe mine . --mcp-families       # Default: consensus across sourcegraph + ast + grep
codeprobe mine . --mcp-families --consensus-threshold 0.9  # Stricter agreement
codeprobe mine . --mcp-families --consensus-backends ast,grep  # Drop a backend
codeprobe mine . --mcp-families --no-consensus  # UNSAFE: legacy single-backend GT
codeprobe mine . --preset quick       # Quick scan: count=3
codeprobe mine . --preset mcp         # MCP eval: org-scale + MCP families + enrich

# Cross-validate after mining
codeprobe mine-cross-validate ./.codeprobe/tasks --threshold 0.6

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

## How to read aggregate output

`codeprobe experiment aggregate` prints per-config metrics and pairwise deltas, and emits `reports/aggregate.json`. It also runs three lightweight bias detectors so silent measurement artifacts don't get reported as real signal.

When a warning fires, it appears above the score table as `[<kind>] <message>` and is mirrored to `aggregate.json` under `bias_warnings`.

| Warning kind              | What it means                                                                              | What to do                                                                                                                       |
| ------------------------- | ------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| `backend_overlap`         | A config's MCP tool surface includes a backend that produced the ground truth.             | Do not report the with-MCP win as tool value — it may be tautological. Use an independent oracle (AST, hand-curated GT) instead. |
| `overshipping`            | The losing config scored ≈0 with recall ≈1.0; it found everything but was over-shipping.   | Likely measures a tool capability boundary, not tool quality. Tighten the loser's tool surface or expand the GT.                 |
| `no_independent_baseline` | Every task's GT comes from a single backend reachable by some configs but not all.         | Aggregate winner is suppressed (pairwise deltas hidden). Mine GT with a different backend before declaring a winner.             |

Pass `--no-warn` to suppress the stdout block and re-enable winner ranking — useful when scripting. The structured `bias_warnings` array is always written to `aggregate.json` regardless.

```bash
codeprobe experiment aggregate ./mcp-comparison
codeprobe experiment aggregate ./mcp-comparison --no-warn   # for CI / pivots
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
