---
name: mine-tasks
description: Mine eval tasks from a repository's history. Extracts real code-change tasks from merged PRs/MRs with ground truth, test scripts, and scoring rubrics. Works with GitHub, GitLab, Bitbucket, Azure DevOps, Gitea, or local repos. Triggers on mine tasks, propose tasks, discover tasks, find tasks, extract tasks, benchmark my repo, eval my repo.
user-invocable: true
---

# Mine Tasks

Point at a codebase and extract real eval tasks from its merge history. Mines merged PRs/MRs to create tasks where agents must reproduce known fixes and features, with auto-generated ground truth for scoring.

Invokes `codeprobe mine` under the hood -- all mining runs through the CLI, not Python imports.

**Note:** The CLI now has its own interactive mode (auto-enabled in TTY). When a user runs `codeprobe mine` directly in a terminal, the CLI handles the interactive workflow (eval goal, config, pre-flight, quality review, results table, next steps). The skill phases below describe the same flow — use the skill when the user invokes `/mine-tasks` from Claude Code, or run the CLI directly.

---

## Phase 0: Eval Goal

Ask the user:

**Question 1** -- Header: "What are you trying to learn?"

- Question: "What's the goal of this evaluation? This determines what kinds of tasks I mine."
- Options:
  - **MCP / tool comparison** -- "Does adding Sourcegraph, code search, or other MCP tools help the agent? I'll mine harder tasks that require cross-file navigation and deep codebase understanding."
  - **Model comparison** -- "Which model performs best (Opus vs Sonnet vs Haiku)? I'll mine a mix of difficulties to find where models diverge."
  - **Prompt / instruction comparison** -- "Which system prompt or instruction style works best? I'll mine a variety of task types."
  - **General benchmarking** -- "Just want to see how well agents handle my codebase. Balanced mix."

Map selection to mining parameters:

| Goal                  | `MIN_FILES`     | Difficulty bias                         | Rationale                                            |
| --------------------- | --------------- | --------------------------------------- | ---------------------------------------------------- |
| MCP / tool comparison | `--min-files 6` | Hard: cross-file, deep navigation       | Easy tasks don't differentiate MCP from agentic grep |
| Model comparison      | `--min-files 2` | Mixed: need variance to separate models |                                                      |
| Prompt comparison     | `--min-files 2` | Mixed                                   |                                                      |
| General benchmarking  | (no filter)     | Balanced                                |                                                      |

---

## Phase 1: Mining Configuration

**Question 2** -- Header: "Target codebase"

- Question: "Which repo should I mine tasks from?"
- Options:
  - **Current directory** -- "Mine from the repo in the current working directory"
  - **Specific path** -- "I'll provide a path to a local repo"

If **Current directory**, set `REPO_PATH=.`.
If **Specific path**, prompt for the absolute path and set `REPO_PATH={user_input}`.

### Validate Path

```bash
git -C {REPO_PATH} rev-parse --git-dir 2>/dev/null && echo "valid" || echo "not a git repo"
```

If not a git repo, ask the user for a different path.

**Question 3** -- Header: "How many tasks?"

- Question: "How many tasks should I mine? (3-20)"
- Options:
  - **Quick look (3-5)** -- "Fast results. Good for a first experiment or validating setup."
  - **Standard (5-10)** -- "Good balance of coverage and speed. Enough to see patterns."
  - **Thorough (10-20)** -- "More statistical confidence. Best for making real tooling decisions."

Map selection to `TASK_COUNT`:

- Quick look: `--count 5`
- Standard: `--count 8`
- Thorough: `--count 15`

**Question 4** -- Header: "Git host"

- Question: "Which git host does this repo use?"
- Options:
  - **Auto-detect** -- "Let codeprobe figure it out from the remote URL"
  - **GitHub** -- "github.com or GitHub Enterprise"
  - **GitLab** -- "gitlab.com or self-hosted GitLab"
  - **Bitbucket** -- "bitbucket.org"
  - **Azure DevOps** -- "dev.azure.com"
  - **Gitea/Forgejo** -- "Self-hosted Gitea or Forgejo instance"
  - **Local only** -- "No remote API access, use git history only"

Map selection to `SOURCE`:

- Auto-detect: `--source auto`
- GitHub: `--source github`
- GitLab: `--source gitlab`
- Bitbucket: `--source bitbucket`
- Azure DevOps: `--source azure`
- Gitea/Forgejo: `--source gitea`
- Local only: `--source local`

---

## Phase 2: Pre-flight Summary

Before mining, show the user a summary of what will happen:

```
Mining plan:
  Goal:       {GOAL}
  Repo:       {REPO_PATH}
  Tasks:      {TASK_COUNT}
  Source:      {SOURCE}
  Min files:   {MIN_FILES} (biasing toward {DIFFICULTY_BIAS} tasks)
```

Confirm before proceeding.

---

## Phase 3: Run Mining

Execute the codeprobe CLI:

```bash
codeprobe mine {REPO_PATH} --count {TASK_COUNT} --source {SOURCE} --min-files {MIN_FILES}
```

This:

1. Connects to the git host API (or falls back to local git log)
2. Discovers merged PRs/MRs with testable code changes
3. Filters for tasks meeting the min-files threshold
4. Sorts by change size (larger changes surface first)
5. Generates task directories with instruction files, ground truth, and scoring rubrics

---

## Phase 4: Quality Review

After mining, review the results critically. Check for these common quality issues:

### Difficulty distribution

Count tasks by difficulty. Flag if the distribution doesn't match the goal:

- **MCP comparison**: should be mostly medium/hard. If >50% easy, warn and suggest re-mining with higher `--min-files`.
- **Model comparison**: should have variance. If all same difficulty, warn.

### Instruction quality

Read each generated `instruction.md`. Flag if:

- Instructions are generic ("reproduce changes from merge X") without describing the problem being solved
- No mention of affected files or the context needed to understand the change
- Missing PR title, issue context, or description of what went wrong / what was needed

If instructions are thin, suggest the user:

1. Look up the original PR/issue for each task and enrich the instruction
2. Or re-mine with `--source github` (or appropriate host) to pull PR descriptions

### Test quality

Check each `tests/test.sh`. Flag if:

- Test scripts are generic stubs (e.g., just `bash tests/test.sh` at repo root)
- No targeted test commands for the specific packages/files affected
- Tests don't actually verify the specific change

If tests are weak, suggest:

1. Replace generic stubs with targeted test commands (e.g., `go test ./pkg/specific/...` or `pytest tests/test_specific.py`)
2. Or use `codeprobe scaffold validate` to check task completeness

### Task diversity

Check if tasks cluster in one area of the codebase. Flag if:

- > 70% of tasks are in the same directory or package
- All tasks are the same language or category
- No variety in task type (all bug fixes, all features, etc.)

---

## Phase 5: Present Results

Display the mining output. For each discovered task, show:

```
Mined {N} tasks:

| # | Task ID              | Category  | Difficulty | Files Changed | Language |
|---|----------------------|-----------|------------|---------------|----------|
| 1 | repo-leak-fix-001    | bug_fix   | hard       | 12            | Go       |
| 2 | repo-auth-feat-001   | feature   | medium     | 7             | Go       |
| 3 | repo-refactor-001    | refactor  | medium     | 5             | Go       |
```

Highlight:

- **Task mix quality** -- Good spread of difficulty and category?
- **Ground truth coverage** -- How many tasks have targeted test scripts vs. generic stubs?
- **Quality warnings** -- Any issues found in Phase 4

---

## Phase 6: Next Steps

```
Tasks mined successfully. Next steps:

  1. Review and enrich task instructions (recommended):
     Look up the original PR for each task and add problem context

  2. Run the eval:
     codeprobe run {REPO_PATH} --agent claude

  3. Try a different model:
     codeprobe run {REPO_PATH} --agent claude --model claude-sonnet-4-6

  4. Set a cost budget:
     codeprobe run {REPO_PATH} --agent claude --max-cost-usd 5.00

  5. Mine more tasks for better statistical confidence:
     codeprobe mine {REPO_PATH} --count 15 --min-files {MIN_FILES}
```

---

## Quick Reference

| User says                            | What happens                                 |
| ------------------------------------ | -------------------------------------------- |
| `/mine-tasks`                        | Mine from current directory, interactive Q&A |
| `/mine-tasks /path/to/repo`          | Mine from specific repo                      |
| "mine hard tasks for MCP comparison" | Mine with `--min-files 6`, bias hard         |
| "mine 10 tasks from this repo"       | Mine with `--count 10`                       |
| "find eval tasks"                    | Same as `/mine-tasks`                        |
| "benchmark my repo"                  | Assess + mine pipeline                       |
