---
name: mine-tasks
description: Mine eval tasks from a repository's history. Extracts real code-change tasks from merged PRs/MRs with ground truth, test scripts, and scoring rubrics. Works with GitHub, GitLab, Bitbucket, Azure DevOps, Gitea, or local repos. Triggers on mine tasks, propose tasks, discover tasks, find tasks, extract tasks, benchmark my repo, eval my repo.
user-invocable: true
---

# Mine Tasks

Point at a codebase and extract real eval tasks from its merge history. Mines merged PRs/MRs to create tasks where agents must reproduce known fixes and features, with auto-generated ground truth for scoring.

Invokes `codeprobe mine` under the hood -- all mining runs through the CLI, not Python imports.

---

## Phase 0: Mining Configuration

Ask the user:

**Question 1** -- Header: "Target codebase"
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

**Question 2** -- Header: "How many tasks?"
- Question: "How many tasks should I mine? (3-20)"
- Options:
  - **Quick look (3-5)** -- "Fast results. Good for a first experiment or validating setup."
  - **Standard (5-10)** -- "Good balance of coverage and speed. Enough to see patterns."
  - **Thorough (10-20)** -- "More statistical confidence. Best for making real tooling decisions."

Map selection to `TASK_COUNT`:
- Quick look: `--count 5`
- Standard: `--count 8`
- Thorough: `--count 15`

**Question 3** -- Header: "Git host"
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

## Phase 1: Run Mining

Execute the codeprobe CLI:

```bash
codeprobe mine {REPO_PATH} --count {TASK_COUNT} --source {SOURCE}
```

This:
1. Connects to the git host API (or falls back to local git log)
2. Discovers merged PRs/MRs with testable code changes
3. Filters for tasks with clear ground truth (patch, test scripts)
4. Generates task directories with instruction files, ground truth, and scoring rubrics

---

## Phase 2: Present Results

Display the mining output. For each discovered task, show:

```
Mined {N} tasks:

| # | Task ID              | Category  | Difficulty | Files Changed | Language |
|---|----------------------|-----------|------------|---------------|----------|
| 1 | repo-leak-fix-001    | bug_fix   | hard       | 3             | Python   |
| 2 | repo-auth-feat-001   | feature   | medium     | 5             | Python   |
| 3 | repo-refactor-001    | refactor  | easy       | 2             | Python   |
```

Highlight:
- **Task mix quality** -- Good spread of difficulty and category?
- **Ground truth coverage** -- How many tasks have automated test scripts vs. rubric-only?
- **Estimated eval cost** -- Rough token estimate for running all tasks

---

## Phase 3: Next Steps

```
Tasks mined successfully. Next steps:

  1. Run the eval:
     codeprobe run {REPO_PATH} --agent claude

  2. Try a different model:
     codeprobe run {REPO_PATH} --agent claude --model claude-sonnet-4-6

  3. Set a cost budget:
     codeprobe run {REPO_PATH} --agent claude --max-cost-usd 5.00

  4. Mine more tasks for better statistical confidence:
     codeprobe mine {REPO_PATH} --count 15
```

---

## Quick Reference

| User says | What happens |
|-----------|-------------|
| `/mine-tasks` | Mine from current directory, interactive Q&A |
| `/mine-tasks /path/to/repo` | Mine from specific repo |
| "mine 10 tasks from this repo" | Mine with `--count 10` |
| "find eval tasks" | Same as `/mine-tasks` |
| "benchmark my repo" | Assess + mine pipeline |
