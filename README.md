# codeprobe

Benchmark AI coding agents against **your own codebase**.

Mine real tasks from your repo history, run agents against them, and find out which setup actually works best for YOUR code — not someone else's benchmark suite.

## Why codeprobe?

Existing benchmarks (SWE-bench, HumanEval) use fixed task sets that AI models may have memorized from training data. codeprobe mines tasks from **your private repo history**, producing benchmarks that are impossible to contaminate.

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
pip install codeprobe            # Core (mine + run + interpret)
pip install codeprobe[stats]     # + statistical tests (scipy)
pip install codeprobe[tokens]    # + exact Copilot token counting (tiktoken)
pip install codeprobe[all]       # Everything

cd /path/to/your/repo

codeprobe assess .      # Score benchmarking potential (optional)
codeprobe init          # What do you want to learn?
codeprobe mine .        # Extract tasks from repo history
codeprobe run .         # Run agents against tasks
codeprobe interpret .   # Get recommendations
```

## Commands

| Command               | Purpose                                     |
| --------------------- | ------------------------------------------- |
| `codeprobe assess`    | Score a codebase's benchmarking potential   |
| `codeprobe init`      | Interactive wizard — choose what to compare |
| `codeprobe mine`      | Mine eval tasks from merged PRs/MRs         |
| `codeprobe run`       | Execute tasks against AI agents             |
| `codeprobe interpret` | Analyze results, rank configurations        |

### Key flags

```bash
codeprobe run . --parallel 5     # Run 5 tasks concurrently (worktree-isolated)
codeprobe run . --repeats 5      # Run each task 5 times for statistical confidence
codeprobe run . --dry-run        # Estimate resource usage without running
codeprobe mine . --enrich        # Use LLM to improve weak task instructions
codeprobe interpret . --format csv   # Export per-task results for pivot tables
codeprobe interpret . --format html  # Self-contained HTML report for leadership
```

## Supported Agents

- **Claude Code** (`--agent claude`) — headless via `claude -p`
- **GitHub Copilot** (`--agent copilot`) — via Copilot CLI
- **Codex** (`--agent codex`) — via OpenAI API
- Custom agents via the `AgentAdapter` protocol

## Supported Git Hosts

GitHub, GitLab, Bitbucket, Azure DevOps, Gitea/Forgejo, and local repos.

## Configuration

Create a `.evalrc.yaml` in your repo root:

```yaml
name: my-experiment
agents: [claude, copilot]
models: [claude-sonnet-4-6, claude-opus-4-6]
tasks_dir: .codeprobe/tasks
```

## License

Apache-2.0
