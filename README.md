# codeprobe

Benchmark AI coding agents against **your own codebase**.

Mine real tasks from your repo history, run agents against them, and find out which setup actually works best for YOUR code — not someone else's benchmark suite.

## Why codeprobe?

Existing benchmarks (SWE-bench, HumanEval) use fixed task sets that AI models may have memorized from training data. codeprobe mines tasks from **your private repo history**, producing benchmarks that are impossible to contaminate.

## Quick Start

```bash
pip install codeprobe            # Core (mine + run + interpret)
pip install codeprobe[stats]     # + statistical tests (scipy)
pip install codeprobe[tokens]    # + exact Copilot token counting (tiktoken)
pip install codeprobe[all]       # Everything

cd /path/to/your/repo

codeprobe init          # What do you want to learn?
codeprobe mine .        # Extract tasks from repo history
codeprobe run .         # Run agents against tasks
codeprobe interpret .   # Get recommendations
```

## Commands

| Command               | Purpose                                     |
| --------------------- | ------------------------------------------- |
| `codeprobe init`      | Interactive wizard — choose what to compare |
| `codeprobe mine`      | Mine eval tasks from merged PRs/MRs         |
| `codeprobe run`       | Execute tasks against AI agents             |
| `codeprobe interpret` | Analyze results, rank configurations        |
| `codeprobe assess`    | Score a codebase's benchmarking potential   |

## Supported Agents

- **Claude Code** (`--agent claude`)
- **GitHub Copilot** (`--agent copilot`)
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
