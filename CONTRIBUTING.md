# Contributing to codeprobe

codeprobe is a Python eval framework for comparing AI coding agents (Claude
Code, Copilot, Codex) on quality, cost, and speed. This document codifies the
process preconditions required by the *Enterprise Repo Benchmark Parity* PRD
(`docs/prd/prd_enterprise_repo_benchmark_parity.md`) — specifically
Process Preconditions P1 (named second reviewer) and P4 (WIP limit).

Read this before opening a PR that lands Phase 2 or Phase 3 work from that PRD.

## Development Setup

1. Clone the repo and `cd` into it.
2. `uv sync` (or `pip install -e '.[dev]'`) — project uses the `src/codeprobe/`
   layout.
3. Run the test suite with `pytest` and the linter with `ruff check .`. Type
   check with `mypy src/codeprobe`.
4. New features follow TDD: write the failing test first, make it pass, refactor.

See `docs/onboarding/architecture_tour.md` for a diagrammed tour of the
pipeline (`mine` → `run` → `aggregate` → `snapshot`) and the per-module entry
points.

## Commit Conventions

- Conventional commit prefixes: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`,
  `chore:`, `perf:`, `ci:`.
- One logical change per commit. Prefer small, frequent commits over megapatches.
- Every commit that changes behaviour has a test that would have failed before
  the change.

## Second Reviewer

> Process Precondition P1 of the Enterprise Repo Benchmark Parity PRD.

All Phase 2 and Phase 3 PRs require review from a **named second reviewer**
before merge. A second reviewer is the project's primary defence against
bus-factor-1 risk (premortem PM8). "Second" means second set of eyes distinct
from the author — the author reviewing themselves does not satisfy this rule.

### Who counts as a second reviewer

A second reviewer is one of the following, in preference order:

1. **A named human reviewer** — another committer or invited collaborator. Async
   review is acceptable; the review does not need to happen in a live call. The
   PR description names the reviewer (`Reviewer: @handle`) and the reviewer
   leaves either an approval or a set of requested changes inside the PR.
2. **Structured Claude Code review as fallback** — when no human reviewer is
   available within 72 hours of PR open, the author may run a structured Claude
   Code review against the diff using a pinned rubric and merge on a green
   outcome. The review must log rubric counts (see below) and attach them to
   the PR body.

Either path satisfies P1 — the PR body must make clear which path was taken and
link to the evidence.

### Claude Code fallback rubric

Structured reviews use the `/review` or `/code-review` command with a pinned
rubric (versioned in `docs/validation/br7_reviewer_prompt.md` and
`docs/validation/br7_ac_standards.md`). Every fallback review logs rubric
counts under at least these four categories:

| Category | What it covers | Count to log |
| --- | --- | --- |
| Correctness | Does the change do what the PR description claims? Tests updated? | `CRITICAL_N`, `HIGH_N`, `MEDIUM_N`, `LOW_N` |
| Safety | Fail-loud-by-default (INV1), tenant isolation (INV2), ZFC boundary (INV3), containerization (INV4), capability contracts (INV5) | `CRITICAL_N`, `HIGH_N`, `MEDIUM_N`, `LOW_N` |
| Tests | Coverage delta, meaningful assertions, chaos/fuzz cases where relevant | `CRITICAL_N`, `HIGH_N`, `MEDIUM_N`, `LOW_N` |
| Docs | Changelog, PRD alignment, module/function docstrings, inline rationale for non-obvious decisions | `CRITICAL_N`, `HIGH_N`, `MEDIUM_N`, `LOW_N` |

Any `CRITICAL_N > 0` blocks merge. `HIGH_N > 0` requires either a fix or an
explicit "ack and defer" response in the PR body that names a follow-up issue.

A fallback review entry lives in the PR body in this shape:

```
Second reviewer: Claude Code structured review (fallback)
Rubric version: br7_reviewer_prompt.md @ <commit-sha>
Correctness: CRITICAL=0 HIGH=1 MEDIUM=2 LOW=3
Safety:      CRITICAL=0 HIGH=0 MEDIUM=1 LOW=0
Tests:       CRITICAL=0 HIGH=0 MEDIUM=0 LOW=1
Docs:        CRITICAL=0 HIGH=0 MEDIUM=1 LOW=0
HIGH items addressed in commits: <sha1>, <sha2>
```

### Scope

- **Phase 0 / Phase 1 PRs**: single-reviewer (author self-merge) acceptable.
- **Phase 2 / Phase 3 PRs**: second reviewer required — no exceptions. A PR
  that lands a Phase 2/3 item without a named second reviewer or a logged
  Claude Code fallback review is reverted on sight.
- **Hotfixes**: a retroactive review inside 24 hours is acceptable if a
  production-blocking bug forced a same-day merge. The PR is still amended
  with reviewer notes before the retroactive window closes.

## WIP Limit

> Process Precondition P4 of the Enterprise Repo Benchmark Parity PRD.

**No more than 2 Phase 2 or Phase 3 items are in flight simultaneously**,
regardless of how parallelizable the individual items look on paper. This
applies to the combined Phase 2 + Phase 3 backlog (R8–R17 plus any
`R*-new` additions), not Phase 2 and Phase 3 independently.

"In flight" means any of: open PR, branch with unmerged commits less than 7
days idle, or an active bead in the `codeprobe-ssf` epic whose status is
`in_progress`. A bead sitting in `backlog` or `ready` does not count. A bead
that has been `in_progress` and untouched for >7 days is considered stalled
and either resumed (still counts) or explicitly moved back to `backlog` (does
not count).

### Why 2, not 3 or 5

Premortem analysis on the PRD's first draft surfaced that the practical
bottleneck on this project is not developer throughput but reviewer
bandwidth plus integration surface. A third concurrent Phase 2/3 item reliably
outpaces the second reviewer and produces drift between items that both touch
the same subsystems (mining curator, trace store, adapters). Two is the
empirically-observable flush rate.

### How the limit is enforced

- `bd list --epic codeprobe-ssf --status in_progress --phase 2-3` returns the
  current count. Any PR that would push the count above 2 is blocked at review
  time until another item lands or is paused.
- The PR template asks the author to list the Phase 2/3 beads they own that
  are currently `in_progress`. A PR author with 2 already-open Phase 2/3
  items must close or pause one before opening a third.
- Phase 0, Phase 1, and Phase 4 work is **not** subject to this limit —
  discovery and publishing polish can fan out as widely as the team has
  capacity for.

### Pausing an item

Prefer pausing over abandoning. To pause, update the bead status to
`paused`, post a summary comment describing what is done / what remains,
and push the working branch so nothing is lost. A paused item does not count
against the WIP limit; resumption re-enters at the back of the queue.

## PR Checklist

Before requesting review:

- [ ] Tests pass locally (`pytest`) and in CI.
- [ ] `ruff check .` and `mypy src/codeprobe` are clean.
- [ ] Coverage delta is non-negative (80 % floor per project rule).
- [ ] Changelog entry added under `CHANGELOG.md` "Unreleased" for user-visible
      changes.
- [ ] For Phase 2/3 PRs: WIP count is ≤ 2 after this PR opens.
- [ ] For Phase 2/3 PRs: second reviewer named in the PR body or structured
      Claude Code review with rubric counts attached.
- [ ] PRD acceptance criteria (if any) referenced by PRD anchor (e.g. `R5.AC1`)
      and explicitly checked off.
- [ ] No hardcoded secrets, API keys, or tenant identifiers in the diff.
- [ ] New heuristics declared against the ZFC compliance section of
      `CLAUDE.md` (new violation, new exception, or refactor of an existing
      violation).

## Security

- Never commit `.env`, credentials, cloud access tokens, or partner-supplied
  sample data into the repo.
- Partner discovery artifacts land under `docs/discovery/` **sanitized** —
  real repo names, engineer names, Slack channel names, internal URLs, and
  cloud project IDs are scrubbed before commit. Partners review the sanitized
  artifact before it merges.
- Vulnerabilities should be reported privately via the security contact in the
  project README rather than via a public issue.

## Communication

- Technical discussion happens on PRs and in the `codeprobe-ssf` beads epic.
- Long-running design threads (≥2 days) are summarized into the PRD's
  `docs/prd/` folder under the relevant filename before the thread closes —
  chat logs alone are not durable.

---

Process preconditions P1 and P4 exist specifically because premortem PM8
("Bus factor of 1; project indistinguishable from abandoned at month 6") was
rated Critical × High across every lens. The cost of a second reviewer and a
WIP cap is small; the cost of an abandoned project is total. Please respect
both.
