# Running codeprobe through a coding agent

codeprobe ships a set of [Claude Code skills](https://docs.claude.com/en/docs/claude-code/skills) that let you drive the full benchmark workflow through conversation instead of the raw CLI. Ask an agent "benchmark this repo" and the skills guide it through mining, running, and interpreting — using the `codeprobe` CLI under the hood.

If you prefer typing commands directly, see [standard.md](./standard.md) instead. The skills do not replace the CLI; they are a user-facing wrapper around it.

## Installing the skills

The skills live in `.claude/skills/` in this repo. Copy them into whichever `.claude/skills/` directory your agent reads from:

```bash
# Option A — install into the repo you want to benchmark (recommended)
mkdir -p /path/to/your/repo/.claude/skills
cp -r /path/to/codeprobe/.claude/skills/* /path/to/your/repo/.claude/skills/

# Option B — install user-wide (available in every project)
mkdir -p ~/.claude/skills
cp -r /path/to/codeprobe/.claude/skills/* ~/.claude/skills/
```

Start (or restart) Claude Code in the target repo. The skills are discovered on startup.

## User-invocable skills

Four skills are meant to be invoked directly. Everything else is internal — `experiment` orchestrates them for you.

| Skill              | When to use                                                                                         |
| ------------------ | --------------------------------------------------------------------------------------------------- |
| `/experiment`      | **Start here.** Guided end-to-end flow: define goal → mine tasks → configure comparison → run → interpret. |
| `/assess-codebase` | Check whether a repo is worth benchmarking (complexity, PR history, test coverage) before committing time. |
| `/interpret`       | Analyze existing eval results — rank configs, compare cost vs. score, surface recommendations.      |
| `/ratings`         | Record and review session quality ratings over time.                                                |

You can also just describe what you want in plain language. The agent picks the right skill automatically:

- *"I want to compare Claude Sonnet vs Opus on my repo"* → `/experiment`
- *"Is this repo good for benchmarking?"* → `/assess-codebase`
- *"What do these results tell me?"* → `/interpret`

## What the full workflow looks like

Starting from `/experiment`, the agent walks you through:

1. **Goal** — what are you trying to learn? (MCP comparison, model comparison, prompt comparison, custom, or "I already have tasks").
2. **Tasks** — mine from PR history, use pre-mined tasks, or generate micro-benchmark probes.
3. **Configurations** — define one or more agent/model/tool combinations to compare.
4. **Run** — execute tasks against each config in parallel, isolated in git worktrees.
5. **Interpret** — rank configs by score and cost-efficiency, generate an HTML report.

Each phase runs a real `codeprobe` CLI command. The agent handles the flag combinations and interprets the output; you approve or adjust at each step.

## When to bypass the skills

Go straight to the CLI (`codeprobe mine`, `codeprobe run`, `codeprobe interpret`) when:

- You're scripting in CI or a non-interactive pipeline.
- You already know the exact flags you want.
- You're debugging a specific codeprobe command and want the unfiltered output.

The skills are for interactive sessions where you want the agent to handle the workflow decisions.

## Troubleshooting

- **Skills don't show up in `/`**: make sure `.claude/skills/<skill-name>/SKILL.md` exists and has valid YAML frontmatter. Restart Claude Code.
- **Agent invokes the wrong skill**: the internal skills (`mine-tasks`, `run-eval`, `scaffold`, `probe`, `acceptance-loop`, `integration-test`) are marked `user-invocable: false` so the agent only reaches them via `/experiment`. If you want direct access, flip that flag to `true` in the relevant `SKILL.md`.
- **`codeprobe: command not found` inside the skill**: the agent shells out to the `codeprobe` CLI. Make sure it's installed in the same environment the agent runs from (`pip install codeprobe`).
