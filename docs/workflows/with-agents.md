# Running codeprobe through a coding agent

codeprobe ships five [Claude Code skills](https://docs.claude.com/en/docs/claude-code/skills) inside the Python package. They let you drive the benchmark workflow through conversation instead of the raw CLI: ask the agent to "benchmark this repo" and it picks the matching skill, which shells out to the `codeprobe` CLI and interprets the output for you.

If you prefer typing commands directly, see [standard.md](./standard.md) instead. The skills do not replace the CLI; they are an agent-facing wrapper around it.

## Installing the skills

The skills ship inside the `codeprobe` wheel, so `pip install codeprobe` is the only prerequisite; no repository checkout is involved.

```bash
pip install codeprobe
codeprobe skills install
```

That writes the packaged `codeprobe-*` skills into `~/.claude/skills/`, so they are available in every project on the machine. That is the default because the repository you benchmark is an *argument* to `codeprobe mine` and `codeprobe run` — the skills are not tied to the directory you installed from, and you should not have to re-install for each repo you point them at.

To scope them to a single repository instead — checking them into that repo, or keeping a pinned copy — install from inside it:

```bash
cd /path/to/your/repo
codeprobe skills install --project   # writes ./.claude/skills/
```

`--dest <path>` takes an explicit directory. Whichever you choose, the command never clobbers local edits: if an existing copy differs from the packaged version, it refuses with `SKILL_INSTALL_CONFLICT` before writing anything, and `--force` overwrites deliberately.

Start (or restart) Claude Code. The skills are discovered on startup.
Before asking a skill to mine or run, check the selected agent path from that repo:

```bash
codeprobe doctor --repo . --agent claude
```

Use `codeprobe doctor --repo . --agent copilot` when GitHub Copilot CLI is the
selected path. Copilot CLI auth can come from `COPILOT_GITHUB_TOKEN`,
`GH_TOKEN`, `GITHUB_TOKEN`, or `gh auth login`.

## The five skills

Every skill is an autonomous agent contract (`user-invocable: false` in its frontmatter), so there is no slash command to type. Describe what you want in plain language, the agent selects the matching skill from your request, and the skill shells out to the corresponding `codeprobe` CLI command.

| Skill                  | What it does                                                                                                                    | Say something like                                    |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------ | ----------------------------------------------------- |
| `codeprobe-mine`       | Mines eval tasks from your repo's merged PR/MR history: real code-change tasks with ground truth, test scripts, and scoring rubrics. | "Mine tasks from this repo", "benchmark my repo"       |
| `codeprobe-run`        | Executes a task suite in isolated per-task sessions, scores with automated tests, and emits NDJSON events plus a terminal envelope. | "Run the eval", "score the agent on these tasks"       |
| `codeprobe-interpret`  | Turns a run output directory into structured analysis: compares configurations statistically and ranks them by score and cost-efficiency. | "Interpret the results", "compare the configurations"  |
| `codeprobe-calibrate`  | Runs the calibration gate on a new curator version and emits a curator profile when the validity thresholds are met.            | "Calibrate the curator", "run the calibration gate"    |
| `codeprobe-check-infra` | Diagnoses mined-task infrastructure for capability drift and offline readiness before a run, including credential-TTL preflight. | "Check infra before this run", "any capability drift?" |

## What the full workflow looks like

codeprobe's comparison is an A/B over MCP servers and tool configurations on the same agent (Claude Code): identical tasks and model, different tool setups, so the score delta isolates what the tooling contributes. A typical conversation-driven session moves through three skills:

1. **Mine.** "Mine tasks from this repo" makes the agent run `codeprobe mine`, extracting a reusable task suite from merged PR history.
2. **Run.** "Run the suite with and without the MCP server" makes it run `codeprobe run` once per tool configuration, each task isolated in its own git worktree.
3. **Interpret.** "What do these results tell me?" makes it run `codeprobe interpret` to rank the configurations by score and cost-efficiency.

Each phase runs a real `codeprobe` CLI command. The agent handles the flag combinations and interprets the output; you approve or adjust at each step.

## When to bypass the skills

Go straight to the CLI (`codeprobe mine`, `codeprobe run`, `codeprobe interpret`) when:

- You're scripting in CI or a non-interactive pipeline.
- You already know the exact flags you want.
- You're debugging a specific codeprobe command and want the unfiltered output.

The skills are for interactive sessions where you want the agent to handle the workflow decisions.

## Troubleshooting

- **Skills don't get picked up**: make sure `~/.claude/skills/<skill-name>/SKILL.md` exists (or `./.claude/skills/...` for a `--project` install) and has valid YAML frontmatter. Restart Claude Code; skills are discovered on startup.
- **Skills out of date after upgrading codeprobe**: the installed copies are inert files, so upgrading the package does not touch them. `codeprobe doctor` warns when they drift from the CLI; re-run `codeprobe skills install` to refresh. If it refuses with `SKILL_INSTALL_CONFLICT` because you edited the installed copies, re-run with `--force` to overwrite them.
- **`codeprobe: command not found` inside a skill**: the skills shell out to the `codeprobe` CLI. Make sure it's installed in the same environment the agent runs from (`pip install codeprobe`).
