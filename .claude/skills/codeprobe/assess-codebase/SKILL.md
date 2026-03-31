---
name: assess-codebase
description: Assess a codebase for AI agent benchmarking potential. Analyzes repo structure, complexity, and history to estimate how well-suited it is for meaningful agent evaluation. Triggers on assess codebase, codebase assessment, evaluate codebase, codebase readiness, benchmark potential.
user-invocable: true
---

# Assess Codebase

Analyze a codebase to determine how well-suited it is for meaningful AI agent benchmarking. Produces a readiness report covering repo structure, complexity, history depth, test infrastructure, and task mining potential.

Invokes `codeprobe assess` under the hood -- all analysis runs through the CLI, not Python imports.

---

## Phase 0: Assessment Goals

Ask the user:

**Question 1** -- Header: "Target codebase"
- Question: "Which codebase should I assess?"
- Options:
  - **Current directory** -- "Assess the repo in the current working directory"
  - **Specific path** -- "I'll provide a path to a local repo"

If **Current directory**, set `REPO_PATH=.`.
If **Specific path**, prompt for the absolute path and set `REPO_PATH={user_input}`.

### Validate Path

Before proceeding, confirm the path is a valid git repo:

```bash
git -C {REPO_PATH} rev-parse --git-dir 2>/dev/null && echo "valid" || echo "not a git repo"
```

If not a git repo, ask the user for a different path.

---

## Phase 1: Run Assessment

Execute the codeprobe CLI:

```bash
codeprobe assess {REPO_PATH}
```

This analyzes:
- Repository structure and size
- Language distribution
- Code complexity signals
- Git history depth and merge activity
- Test infrastructure coverage
- Build system and CI presence

---

## Phase 2: Present Results

Display the assessment output to the user. Highlight:

1. **Benchmarking potential** -- Is this repo a good candidate for agent evaluation?
2. **Task mining readiness** -- Does the repo have enough merge history and test coverage for `/mine-tasks`?
3. **Key strengths** -- What makes this repo good for benchmarking (e.g., rich PR history, strong test suite)
4. **Gaps** -- What's missing that would improve benchmarking quality (e.g., no CI, sparse test coverage)

---

## Phase 3: Next Steps

Based on the assessment, suggest concrete follow-up actions:

```
Suggested next steps:

  1. {If repo scores well}: Run `codeprobe mine {REPO_PATH}` to extract eval tasks
     from merged PRs.

  2. {If test coverage is low}: Consider adding tests before benchmarking --
     agents can't be scored without a ground truth.

  3. {If history is shallow}: The repo needs more merged PRs for meaningful
     task mining. Consider using a more active repo.
```

---

## Quick Reference

| User says | What happens |
|-----------|-------------|
| `/assess-codebase` | Assess current directory |
| `/assess-codebase /path/to/repo` | Assess specific repo |
| "is this repo good for benchmarking?" | Same as `/assess-codebase` |
| "evaluate my codebase" | Same as `/assess-codebase` |
