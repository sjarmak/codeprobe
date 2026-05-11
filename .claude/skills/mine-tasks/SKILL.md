---
name: mine-tasks
description: Mine eval tasks from a repository's history. Extracts real code-change tasks from merged PRs/MRs with ground truth, test scripts, and scoring rubrics. Works with GitHub, GitLab, Bitbucket, Azure DevOps, Gitea, or local repos. Triggers on mine tasks, propose tasks, discover tasks, find tasks, extract tasks, benchmark my repo, eval my repo.
user-invocable: false
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

| Goal                  | Mining mode                                       | Rationale                                            |
| --------------------- | ------------------------------------------------- | ---------------------------------------------------- |
| MCP / tool comparison | `--org-scale --mcp-families` (see MCP flow below) | Org-scale comprehension tasks designed for MCP delta |
| Model comparison      | SDLC `--min-files 2`, mixed difficulty            | Need variance to separate models                     |
| Prompt comparison     | SDLC `--min-files 2`, mixed difficulty            | Variety of task types                                |
| General benchmarking  | SDLC (no filter), balanced                        | Broad coverage                                       |

### MCP / Tool Comparison Flow

When the user selects MCP comparison, switch to org-scale mining with MCP families:

**Question 2b** -- Header: "Sourcegraph repo identifier"

> If the caller (e.g. `/experiment` Step 0d) supplied `SG_REPO`, **skip this question** and use the supplied value verbatim — see the **Caller contract** subsection of Distribution-Driven Mode below.

- Question: "What's the Sourcegraph repo name? (e.g., github.com/sg-evals/kubernetes-api)"
- This is needed so the ground truth can be enriched via Sourcegraph `find_references`, and so the preamble knows which repo to scope queries to.
- If user doesn't know, derive from the repo's git remote: parse origin URL (`git@github.com:OWNER/REPO` or `https://github.com/OWNER/REPO`) and propose `github.com/OWNER/REPO`. **Credential safety:** if the URL contains a `userinfo@` component (e.g. `https://TOKEN@github.com/...`), treat the parse as failed — do not echo the raw URL. When the remote is unparsable AND `--mcp-families` is selected, the CLI's org-scale default `github.com/sg-evals/{repo_name}` applies (no origin probe in that path).
- **Validate** the value matches `^[A-Za-z0-9._/-]+$` (no spaces, no shell metacharacters). Re-prompt on mismatch.

**Question 2c** -- Header: "Which MCP families?"

> If the caller (e.g. `/experiment` Step 0f) supplied `TASK_DISTRIBUTION` as a non-`auto` JSON object, **skip this question** — see the **Distribution-Driven Mode** section below for routing. Only ask when distribution is `auto`.

- Options (all selected by default):
  - **symbol-reference-trace** -- Find all files referencing a symbol (catches aliases, re-exports)
  - **type-hierarchy-consumers** -- Find implementations and consumers of base classes
  - **change-scope-audit** -- Blast radius: all files affected by changing a symbol
- Or: **All MCP families** (default)

Then run (always `--no-llm` — enrichment happens via subagent below):

```bash
source .env.local 2>/dev/null  # Load SOURCEGRAPH_ACCESS_TOKEN if present
export SOURCEGRAPH_TOKEN="${SOURCEGRAPH_ACCESS_TOKEN:-}"

codeprobe mine {REPO_PATH} --org-scale --mcp-families \
  --count {TASK_COUNT} --no-interactive --no-llm \
  --family {SELECTED_FAMILIES...} \
  --sg-repo {SG_REPO}
```

### Post-mine enrichment (subagent)

After mining completes, spawn a subagent to enrich each task's `instruction.md`:

1. For each task directory under `{REPO_PATH}/tasks/`:
   - Read the generated `instruction.md` and `ground_truth.json`
   - Rewrite the instruction to be a clear, challenging discovery question that does NOT leak file paths or pattern hints from the ground truth
   - Assess difficulty based on the ground truth scope (file count, cross-directory spread, multi-hop reasoning required) and update `metadata.json`
2. This runs inside the existing Claude Code session — no API key needed.

Skip Phase 1 questions about git host (not needed for org-scale mining).

---

## Distribution-Driven Mode

When invoked from `/experiment` with `TASK_DISTRIBUTION` set to a JSON object (e.g. `{"symbol-reference-trace": 5, "sdlc": 5, "oracle_checks": 5}`), bypass the goal-bound default and mine per-family per the supplied counts.

### Caller contract

Variables `/experiment` (or any orchestrating caller) may supply when delegating to `/mine-tasks`. Each variable has an explicit fallback when unset, so the skill remains usable standalone.

| Variable             | Supplied by                  | Format / validation                                  | When unset                                                                                              |
| -------------------- | ---------------------------- | ---------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `REPO_PATH`          | `/experiment` Phase 2a       | Absolute or relative filesystem path                 | Skill asks Question 2 ("Target codebase"). Required — mining cannot proceed without a path.             |
| `TASK_COUNT`         | `/experiment` Step 0e (mapped from `TASK_COUNT_TARGET`) | Positive integer                                     | Skill asks Question 3 ("How many tasks?").                                                              |
| `TASK_DISTRIBUTION`  | `/experiment` Step 0f        | `"auto"` or JSON object `{family: count}`            | Skill falls through to goal-bound default (e.g. `--mcp-families` for Goal 1) — see Phase 0.             |
| `SG_REPO`            | `/experiment` Step 0d        | `^[A-Za-z0-9._/-]+$` (no spaces, no shell metachars) | Skill asks Question 2b (only in MCP / Tool Comparison Flow). For non-MCP families the CLI auto-derives from the repo's origin remote. |

**Skip rules when caller-supplied:**

- `SG_REPO` set → Question 2b is skipped; the supplied value is used verbatim **after format validation** (`^[A-Za-z0-9._/-]+$`). The value is double-quoted in every CLI invocation (`--sg-repo "{SG_REPO}"`) as a defense-in-depth layer; reject if validation fails rather than passing through to the shell.
- `TASK_DISTRIBUTION` is a non-`auto` JSON object → Question 2c (MCP families) is skipped; routing follows the Family → mining mode mapping below.

### Family → mining mode mapping

`{SG_REPO}` is interpolated when set (per the Caller contract); double-quote it in every invocation (`--sg-repo "{SG_REPO}"`). When unset, the `--sg-repo` argument is **omitted** and the CLI applies its own per-path fallback: SDLC families auto-derive from the origin remote via `resolve_sg_repo_from_origin` (`github.com/{owner}/{repo}`), while `--org-scale --mcp-families` jumps directly to `github.com/sg-evals/{repo_name}` with no origin probe (`src/codeprobe/cli/mine_cmd.py:2691`).

| Task family                  | Mining invocation                                                    | Status |
| ---------------------------- | -------------------------------------------------------------------- | ------ |
| `symbol-reference-trace`     | `--org-scale --mcp-families --family symbol-reference-trace --count N --sg-repo {SG_REPO}` | Wired |
| `type-hierarchy-consumers`   | `--org-scale --mcp-families --family type-hierarchy-consumers --count N --sg-repo {SG_REPO}` | Wired |
| `change-scope-audit`         | `--org-scale --mcp-families --family change-scope-audit --count N --sg-repo {SG_REPO}` | Wired |
| `mcp-fbeta`                  | `--org-scale --mcp-families --family symbol-reference-trace --count N --sg-repo {SG_REPO}` + write `verification.fbeta_beta` per task | Wired |
| `org-scale-tier-weighted`    | `--org-scale --count N --sg-repo {SG_REPO}`                          | Wired |
| `org-scale-recall-tilted`    | `--org-scale --recall --count N --sg-repo {SG_REPO}`                 | Wired |
| `dependency-chain`           | `--min-files 3 --bias dependency-chain --count N --sg-repo {SG_REPO}` | Wired (general SDLC bias) |
| `sdlc`                       | `--min-files 2 --count N --sg-repo {SG_REPO}`                        | Wired |
| `binary-test`                | `--min-files 2 --bias binary --count N --sg-repo {SG_REPO}`          | Wired (general SDLC bias) |
| `continuous`                 | `--min-files 2 --bias continuous --count N --sg-repo {SG_REPO}`      | Wired (general SDLC bias) |
| `exact-match`                | `--min-files 2 --bias exact-match --count N --sg-repo {SG_REPO}`     | Wired (general SDLC bias) |
| `dual-composite`             | `--min-files 2 --bias dual-composite --count N --sg-repo {SG_REPO}`  | Wired (general SDLC bias) |
| `oracle_checks`              | _no dedicated miner yet_                                              | **Hardcoded for follow-up** — port the CSB rubric-builder. Track as successor to `codeprobe-bln9`. |

### Execution

For each `(family, count)` entry in `TASK_DISTRIBUTION`:

1. If status is **Wired**, run the mapped invocation, accumulating into the experiment's tasks directory.
2. If status is **Hardcoded for follow-up**, append the family name to `MINING_HARDCODED_FOR` and skip.

After all entries process:

```
Mining summary:
  Honored:  symbol-reference-trace=5, sdlc=5
  Skipped:  oracle_checks=5  (no miner yet, follow-up to bln9)
  Total tasks produced: 10 (target was 15)
```

If `MINING_HARDCODED_FOR` is non-empty, surface it explicitly to the caller so the experiment can decide whether to proceed with the partial set or bail.

### Backward compatibility

If `TASK_DISTRIBUTION` is absent, `null`, or `"auto"`, this section is skipped entirely and mining proceeds via the goal-bound default in Phase 0. No existing invocation breaks.

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
