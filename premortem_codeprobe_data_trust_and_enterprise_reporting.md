# Premortem: Codeprobe Data Trust & Enterprise Reporting

## Risk Registry

| #   | Failure Lens               | Severity     | Likelihood | Risk Score | Root Cause                                                                                                                                    | Top Mitigation                                                                                      |
| --- | -------------------------- | ------------ | ---------- | ---------- | --------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| 1   | Technical Architecture     | Critical (4) | High (3)   | **12**     | Worktree isolation only covers filesystem, not agent runtime state (~/.claude/, ~/.codex/)                                                    | Add `isolate_session()` to AgentAdapter protocol; resolve Open Question #1 as blocking gate         |
| 2   | Security & Compliance      | Critical (4) | High (3)   | **12**     | No env whitelist on agent subprocesses; sandbox detection deferred while dangerous permissions planned                                        | Restrict subprocess env via whitelist; gate dangerously_skip on sandbox detection                   |
| 3   | Operational                | Critical (4) | High (3)   | **12**     | No resource governor — nested ThreadPoolExecutors create multiplicative parallelism (configs × parallel × repeats)                            | Global concurrency semaphore; disk-space pre-checks; error taxonomy                                 |
| 4   | Integration & Dependencies | Critical (4) | High (3)   | **12**     | Silent fallbacks mask format changes (Copilot NDJSON, tiktoken API, scipy deprecations)                                                       | Format contract tests with golden fixtures; replace silent fallbacks with loud warnings             |
| 5   | Scope & Requirements       | Critical (4) | High (3)   | **12**     | Reports optimized for statistical correctness, not decision utility — customers misinterpret CI bars and cross-billing-model cost comparisons | Define 3 decision questions before building reports; enforce billing-model separation in data model |

## Cross-Cutting Themes

### Theme 1: Agent Session State Isolation (Arch + Security + Ops)

Three lenses independently identified that git worktrees are necessary but insufficient for isolation. The architecture lens found `~/.claude/` session corruption. The security lens found full environment inheritance leaking API keys across configs. The operational lens found concurrent session state causing non-deterministic scores. **This is the #1 risk**: the PRD's flagship feature (worktree isolation) has a fundamental design gap that three independent failure analyses converged on.

**Combined severity**: Catastrophic — corrupted results, leaked secrets, and wasted budget simultaneously.

### Theme 2: Silent Degradation Instead of Loud Failure (Deps + Ops + Security)

Three lenses found that the codebase's error handling pattern is "catch and continue" rather than "fail fast." Dependencies lens: `copilot.py:78` broad `except` swallows format changes. Operations lens: `scoring.py` catches `OSError` and returns 0.0. Security lens: MCP temp file cleanup only runs in happy path. **The system will break silently, producing plausible-looking garbage**, which is worse than crashing.

**Combined severity**: Critical — enterprise customers receive authoritative-looking reports computed on garbage data.

### Theme 3: Multiplicative Resource Explosion (Arch + Ops)

Both architecture and operations lenses identified that `configs × parallel × repeats` creates an explosion in concurrent processes, worktrees, disk usage, and budget consumption that the executor was never designed to handle. The budget circuit-breaker uses `future.cancel()` (a no-op on running tasks) and only checks cost after completion.

**Combined severity**: High — OOM kills, disk exhaustion, and 3-4x budget overruns in production.

### Theme 4: Report Misinterpretation (Scope + Deps)

Scope and dependencies lenses both found that technically correct reports lead to incorrect conclusions. Wilson CIs are misread as measurement reliability. Cost comparisons mix billing models. Pricing tables go stale. The statistical machinery gives false precision to broken measurements.

**Combined severity**: Critical — the $100K tooling decisions the PRD targets become $100K mistakes.

## Mitigation Priority List

Ranked by: failure modes addressed × severity × (inverse) implementation cost.

| Priority | Mitigation                                                                                             | Failure Modes       | Effort            |
| -------- | ------------------------------------------------------------------------------------------------------ | ------------------- | ----------------- |
| **P0**   | Restrict agent subprocess env via whitelist (like `scoring.py:_safe_env()`)                            | Security, Arch      | Low (~5 lines)    |
| **P0**   | Resolve Open Question #1 before shipping R1 — test parallel Claude sessions for `~/.claude/` conflicts | Arch, Security, Ops | Low (experiment)  |
| **P0**   | Add `isolate_session()` to AgentAdapter protocol — per-adapter session dir isolation                   | Arch, Security      | Medium            |
| **P0**   | Global concurrency semaphore capping total active subprocesses across all configs                      | Ops, Arch           | Medium            |
| **P1**   | Replace silent fallbacks with loud warnings + error fields (copilot.py:78, scoring.py)                 | Deps, Ops           | Low               |
| **P1**   | Fix MCP temp file lifecycle — use context manager with finally cleanup + atexit handler                | Security            | Low               |
| **P1**   | Remove `symlinks=True` from scoring copytree                                                           | Security            | Low (1 line)      |
| **P1**   | Gate `dangerously_skip` permission mode on `is_sandboxed()` — make R12 prerequisite                    | Security            | Medium            |
| **P1**   | Enforce billing-model separation in data model, not just UI — add `billing_model` field                | Scope               | Medium            |
| **P1**   | Define 3 decision questions the HTML report must answer before building R8                             | Scope               | Low (design work) |
| **P2**   | Format contract tests with golden fixtures for Copilot NDJSON and Claude JSON                          | Deps                | Medium            |
| **P2**   | Pre-dispatch cost reservation (reserve estimated_cost × parallel before launching)                     | Arch, Ops           | Medium            |
| **P2**   | Disk-space pre-checks before worktree creation and scoring copytree                                    | Ops                 | Low               |
| **P2**   | Error taxonomy: add `error_category` to CompletedTask (agent/system/timeout/resource)                  | Ops                 | Low               |
| **P2**   | `--dry-run` flag computing total resource requirements before execution                                | Ops                 | Medium            |
| **P2**   | Pin scipy and tiktoken with upper bounds in pyproject.toml                                             | Deps                | Low               |
| **P2**   | Add pricing staleness detection (warn if >90 days old)                                                 | Deps                | Low               |
| **P2**   | Cross-config isolation integration test (canary file)                                                  | Security, Arch      | Medium            |
| **P2**   | No Wilson CI bars in HTML when `--repeats 1` — point estimates only with banner                        | Scope               | Low               |
| **P3**   | Enrichment validation gate — verify enrichment doesn't shift task difficulty                           | Scope               | Medium            |
| **P3**   | Extensible secret redaction patterns via experiment config                                             | Security            | Low               |
| **P3**   | Report interpretation test — show to non-statistician, verify correct conclusion                       | Scope               | Medium            |
| **P3**   | Version-gate CLI integrations with version checks in preflight()                                       | Deps                | Medium            |

## Design Modification Recommendations

### 1. Full Execution Isolation, Not Just Filesystem Isolation

**What to change**: Expand `IsolationStrategy` from "worktree management" to "execution environment management." Each parallel slot gets: (a) its own git worktree, (b) its own agent session directory (via `CLAUDE_CONFIG_DIR`, etc.), (c) a restricted env whitelist, (d) a dedicated temp directory for MCP configs. Add `isolate_session()` to the `AgentAdapter` protocol as a required method.

**Failure modes addressed**: Technical Architecture (#1), Security (#2), Operational (#3)

**Effort**: Medium — extends R1 design, requires per-adapter session isolation research.

### 2. Resource Governor with Global Concurrency Control

**What to change**: Replace nested `ThreadPoolExecutor` pools (config-level + task-level) with a single global `Semaphore(max_concurrent)`. Add pre-dispatch budget reservation. Add disk-space pre-checks. Add `--dry-run` flag. Add process-level kill (not `future.cancel()`) for budget breaker.

**Failure modes addressed**: Operational (#3), Technical Architecture (#1)

**Effort**: Medium — refactors executor.py parallelism model.

### 3. Fail-Loud Error Philosophy

**What to change**: Replace all broad `except` clauses that silently degrade with specific catches that (a) log at WARNING, (b) set `error` field on output, (c) flag in reports. Add error taxonomy (`error_category`) to `CompletedTask`. Add format contract golden-file tests. Surface any config with >30% error tasks as a system failure, not task failure.

**Failure modes addressed**: Integration (#4), Operational (#3), Security (#2)

**Effort**: Low-Medium — many small changes across adapters and scoring.

### 4. Decision-Oriented Report Design

**What to change**: Before building R8 (HTML), define the 3 questions the report answers. Add `billing_model` to `ConfigSummary` and refuse to rank agents across billing models in a single table. Suppress Wilson CI visualizations when `--repeats 1`. Add "report interpretation test" to R8 acceptance criteria.

**Failure modes addressed**: Scope (#5), Integration (#4 — stale pricing becomes visible)

**Effort**: Low — mostly design decisions, not code.

### 5. Sandbox-First Security Model

**What to change**: Restrict subprocess env immediately (P0, 5 lines). Fix MCP temp file cleanup. Remove `symlinks=True` from scoring. Make R12 (sandbox detection) a prerequisite for `dangerously_skip`, not Nice-to-Have. Add cross-config isolation test.

**Failure modes addressed**: Security (#2)

**Effort**: Low — the individual fixes are small; the discipline change (R12 as prerequisite) is the hard part.

## Full Failure Narratives

### 1. Technical Architecture Failure

Git worktree isolation only covers the repository filesystem. Claude Code's `~/.claude/` session state, Copilot's global config, and Codex's runtime files create crosstalk channels invisible to worktree boundaries. Multiple parallel sessions corrupt each other's state, producing non-deterministic scores. The budget circuit-breaker uses `future.cancel()` (a no-op) and only checks cost post-completion, causing 3-4x budget overruns under parallelism. Repeat infrastructure compounds both problems: `--repeats 5 --parallel 5` means 25 concurrent processes per config.

### 2. Integration & Dependency Failure

Copilot CLI 2.0 changes NDJSON format; `copilot.py:78` broad except swallows the change silently. tiktoken 1.0 drops `gpt-4o` model string. scipy deprecates Wilcoxon parameter defaults. Hardcoded pricing tables go stale. All three hit within 6 months. The compound effect: enterprise customer gets wrong token counts, wrong costs, and inconsistent p-values — all presented with confident Wilson CIs.

### 3. Operational Failure

Nested ThreadPoolExecutors create multiplicative parallelism (configs × parallel × repeats = 75 concurrent processes). OOM kills terminate agents mid-run. Checkpoint store excludes errors from retry list, causing infinite retry of doomed tasks. Worktree working trees fill /tmp. SQLite WAL contention drops checkpoint entries. Customers receive HTML reports with narrow CIs computed on 60% error-rate data.

### 4. Scope & Requirements Failure

Wilson CIs are misread as measurement reliability ("will I get this result if I re-run?"). Cost comparisons mix subscription (Copilot) and per-token (Claude) billing models in one ranking table. LLM-enriched tasks shift difficulty, not just clarity. The report is statistically correct but leads non-statisticians to wrong conclusions — the exact opposite of the PRD's goal.

### 5. Security & Compliance Failure

Agent subprocesses inherit full parent environment (API keys, tokens). MCP temp files persist with expanded secrets after crashes. Scoring copytree follows symlinks into other worktrees. `dangerously_skip` ships without sandbox detection. Secret redaction regex misses customer-specific credential formats. Enterprise customer's private code and credentials leak across config boundaries.
