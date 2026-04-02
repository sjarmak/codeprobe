# PRD: Codeprobe Data Trust & Enterprise Reporting

## Problem Statement

Codeprobe produces eval results that cannot be trusted for enterprise decisions. Agents running in parallel share the same working directory, corrupting each other's files. Single runs yield no statistical confidence — there are no repeat runs, no confidence intervals, no effect sizes. The reporting layer outputs one paragraph where customers making $100K tooling decisions need per-task breakdowns, token comparisons, and actionable statistical analysis. Token counts for Copilot are estimated at ~4 chars/token (30-50% error), costs are calculated using the wrong pricing model (Claude Sonnet rates for Copilot), and the `cost_source` field that should distinguish precise from approximate data is misused. Half the mined tasks have weak instructions that produce noisy results.

The dependency chain is: **task isolation → repeats → reports**. Without clean isolated runs, repeats amplify noise. Without repeats, reports can't show confidence intervals. Quality gates and Claude headless fixes are independent and can be parallelized.

## Goals & Non-Goals

### Goals

- Produce statistically trustworthy eval results with quantified uncertainty
- Enable parallel task execution without filesystem interference
- Deliver enterprise-grade reports sufficient for $100K tooling decisions
- Make token counts and cost attribution accurate and transparent
- Ensure mined tasks have sufficient instruction quality for meaningful evals

### Non-Goals

- Building a web dashboard or SaaS platform (self-contained HTML is sufficient)
- Supporting non-git repositories (worktree isolation requires git)
- Real-time streaming of eval progress (batch reporting is sufficient)
- Supporting arbitrary agent types beyond Claude, Copilot, and Codex
- PDF report generation or "decision memo" format

## Requirements

### Must-Have

- **R1: Git worktree isolation for parallel execution**
  - Acceptance: Running `codeprobe run --parallel 3` on a repo where two tasks modify the same file produces correct scores for all tasks. Each task runs in its own git worktree. Worktrees are cleaned up after scoring.
  - Implementation: `IsolationStrategy` protocol in `core/isolation.py` with `WorktreeIsolation` default. Pool of N worktrees matching `--parallel` count. Prompt `repo_path` rewritten to worktree path.
  - **PREMORTEM P0**: Worktree isolation is necessary but NOT sufficient. Must also isolate agent session state (`~/.claude/`, `~/.codex/`) via per-slot temp directories and `CLAUDE_CONFIG_DIR` overrides. Add `isolate_session()` to `AgentAdapter` protocol. Resolve Open Question #1 (parallel Claude session conflicts) BEFORE implementation — this is a blocking gate, not a nice-to-have. See premortem themes 1 & 3.
  - **PREMORTEM P0**: Restrict agent subprocess environment via whitelist (like `scoring.py:_safe_env()`). Never inherit parent's full env — API keys leak across configs. ~5 lines in `_base.py:100`.
  - **PREMORTEM P0**: Add global concurrency semaphore. Nested ThreadPoolExecutors (config-level × task-level) create multiplicative parallelism. `--parallel 5` with 3 configs and 5 repeats = 75 concurrent processes. Cap total active subprocesses globally.
  - **PREMORTEM P1**: Remove `symlinks=True` from `scoring.py:154` copytree — sandbox escape vector in worktree context. Fix MCP temp file lifecycle (secrets persist on crash).

- **R2: Git reset between sequential tasks**
  - Acceptance: Running `codeprobe run --parallel 1` on 10 tasks where each modifies files produces identical scores regardless of task order. `git checkout -- . && git clean -fd` executes between tasks.

- **R3: Repeat run infrastructure**
  - Acceptance: `codeprobe run --repeats 5` executes each task 5 times per config. `CompletedTask` includes `repeat_index: int`. Results directory contains `{task_id}/run_{0..4}/` subdirectories. `codeprobe interpret` aggregates across repeats.

- **R4: Statistical hypothesis testing in analysis layer**
  - Acceptance: `PairwiseComparison` includes `p_value: float | None`, `effect_size: float | None`, `effect_size_method: str` (cliff_delta for binary, cohen_d for continuous), `ci_lower: float`, `ci_upper: float`. McNemar's exact test for binary pass/fail, Wilcoxon signed-rank for continuous scores. Wilson score intervals for pass rates. Sample-size warning when N < 10.
  - Dependencies: `scipy.stats` (for `wilcoxon`); McNemar implementable in ~15 lines without deps.
  - **PREMORTEM P2**: Pin `scipy>=1.11,<2` with upper bound. Deprecation cycles break Wilcoxon parameter defaults across versions. Add golden-file test fixtures for statistical output stability.
  - **PREMORTEM P2**: No Wilson CI bars in HTML when `--repeats 1` — show point estimates only with banner. Prevents misinterpretation as measurement reliability (premortem scope finding).

- **R5: Fix cost_source attribution**
  - Acceptance: When `NdjsonStreamCollector` uses `_estimate_tokens()` heuristic, `cost_source` is set to `"estimated"`, not `"calculated"`. Reports annotate configs with estimated costs.

- **R6: Decouple Copilot cost model from Claude pricing**
  - Acceptance: `COPILOT_PRICING` table exists with GPT-4o rates. When Copilot model is unknown, `cost_source="estimated"`. Reports clearly label subscription-based vs per-token cost comparisons.
  - **PREMORTEM P2**: Add `_PRICING_LAST_VERIFIED` date constant. Warn in reports if pricing data is >90 days old. OpenAI changes pricing quarterly.
  - **PREMORTEM P1**: Add `billing_model: Literal["per_token", "subscription", "hybrid"]` to `ConfigSummary`. Refuse to rank agents across billing models in a single table without explicit `cost_override`.

### Should-Have

- **R7: Per-task breakdown and CSV export**
  - Acceptance: `codeprobe interpret --format csv` produces per-task rows with columns: `config, task_id, repeat, score, pass, duration_sec, cost_usd, cost_source, input_tokens, output_tokens, cache_read_tokens, cost_model`. Text report includes per-task table. JSON report includes full per-task data.

- **R8: Self-contained HTML report**
  - Acceptance: `codeprobe interpret --format html` produces a single `.html` file (inline CSS/JS, no external deps) with: executive summary, ranking table, per-task drill-down, pairwise comparison cards with CI bars, cost-efficiency section separating per-token from subscription agents.
  - **PREMORTEM P1**: Define 3 concrete decision questions the report must answer BEFORE building. Add "report interpretation test" to acceptance criteria — show to non-statistician, verify correct conclusion. Suppress CI bars when `--repeats 1`.

- **R9: tiktoken integration for Copilot/OpenAI token counting**
  - Acceptance: When `tiktoken` is installed (optional dependency), Copilot input tokens are counted via `tiktoken.encoding_for_model()` instead of 4-chars/token. Falls back to heuristic when tiktoken unavailable. Token count accuracy improves from ~70% to ~99% for GPT-4o.

- **R10: LLM-based task enrichment**
  - Acceptance: `codeprobe mine --enrich` calls `core/llm.py:call_claude()` with PR diff + commit messages for tasks with quality score < 0.5. Enriched tasks include `enrichment_source: "llm"` in metadata. `--enrich` is off by default.

- **R11: Persist quality_score in TaskMetadata**
  - Acceptance: `metadata.json` includes `quality_score: float`. Downstream analysis can correlate task quality with eval reliability.

### Nice-to-Have

- **R12: Sandbox detection for `--dangerously-skip-permissions`**
  - Acceptance: `is_sandboxed()` checks `/.dockerenv`, `CODEPROBE_SANDBOX=1` env var, cgroup namespaces. `permission_mode: "dangerously_skip"` raises `AdapterSetupError` when not sandboxed. Note: `auto` mode already handles most eval scenarios.
  - **PREMORTEM P1 — PROMOTED FROM NICE-TO-HAVE**: R12 MUST ship before `dangerously_skip` is added to `ALLOWED_PERMISSION_MODES`. Without sandbox detection, adding the permission mode is a one-line change with no safety gate. Encode as code-level assertion, not documentation promise.

- **R13: Power analysis calculator**
  - Acceptance: `codeprobe power --effect-size 0.15 --alpha 0.05 --power 0.8` outputs required task count and repeat count. Helps users plan experiments before running them.

- **R14: Deprecate unused `token_count` field**
  - Acceptance: `AgentOutput.token_count` and `CompletedTask.token_count` removed. No adapter sets this field (always None). Replaced by `input_tokens` + `output_tokens`.

## Design Considerations

### Task isolation: worktrees vs containers vs temp copies

Git worktrees are the right default: O(1) creation cost (shared object store), no Docker dependency, full git functionality inside the worktree. `shutil.copytree` is prohibitive for large repos. Containers add deployment complexity. The pool-of-worktrees pattern (N worktrees matching `--parallel` count, reset between tasks) amortizes creation cost.

**Critical subtlety**: The preamble prompt embeds `repo_path` (preamble.py line 83). Any isolation mechanism must rewrite this to the worktree path, or agents using `Bash(command="ls /original/path")` will escape isolation.

### Effect size: Cliff's delta vs Cohen's d

The codebase predominantly produces binary (0/1) pass/fail outcomes. Cohen's d is unreliable for binary data because the pooled standard deviation is mechanically tied to the proportion. Cliff's delta is the correct non-parametric effect size for ordinal/binary data. Cohen's d should only be used for continuous scores from `ContinuousScorer`.

### Copilot cost model

Copilot is subscription-based. Per-token cost comparisons against API-billed agents (Claude, Codex) are inherently apples-to-oranges. Reports must either: (a) separate subscription agents into a distinct section, or (b) allow users to input an amortized per-token cost for subscription agents. The current approach of using Claude Sonnet pricing for Copilot silently produces misleading results.

### Existing paired infrastructure

The `experiment_cmd.py` already matches tasks by `task_id` across configs and computes per-task deltas. This is the foundation of a paired statistical test — the infrastructure is 80% built. Adding the actual test (McNemar's or Wilcoxon) is a small code change with large credibility impact.

### Skill documentation gap

The interpret SKILL.md describes Cohen's d, confidence intervals, per-task tables, and HTML reports that do not exist in code. **Update SKILL.md immediately to match current reality** (converge: resolved, all positions agreed). Implement described features as infrastructure lands.

### Wilson CIs on single-run data (converge: conditionally resolved)

Wilson score intervals on single-run pass rates are mathematically valid for task-sampling uncertainty ("given this sample of tasks, what's the plausible true pass rate?"). However, they do NOT measure run-to-run measurement reliability, which is the dominant source of uncertainty and what enterprise customers naturally expect CIs to represent. **Ship Wilson CIs only with explicit labeling: "Task-sampling uncertainty. Use --repeats N for measurement reliability estimates."** This prevents the subtle statistical error of conflating sampling variance with measurement variance.

### Reports as diagnostic infrastructure (converge: new insight)

Per-task breakdowns serve a dual purpose: enterprise reporting AND validation tooling. Comparing per-task scores across parallel vs sequential runs can verify worktree isolation is working correctly. This reframes reports from a dependent output layer to an observability layer that enables foundation work.

## Implementation Phases (revised after converge debate)

Three parallel tracks, converging at week 3. Key principle: "independent in the PRD means ship independently."

### Track A: Stop the Bleeding (week 1)

Ship immediately — zero dependency on foundation work:

- R5: `cost_source="estimated"` fix (1 line at `telemetry.py:234`)
- R6: Decouple Copilot pricing from Claude Sonnet rates
- R2: Git reset between sequential tasks
- R14: Deprecate unused `token_count` field
- SKILL.md: Update to match current implementation reality

### Track B: Foundation (weeks 1-4)

- Week 1-2: R1 (worktree isolation) — `IsolationStrategy` protocol, worktree pool, prompt path rewriting
- Week 3: R3 (repeat infrastructure) — `repeat_index` on `CompletedTask`, `--repeats N` flag
- Week 4: R4 full (McNemar's, Cliff's delta, Wilcoxon) — flows into existing report formats

### Track C: Reporting (weeks 1-4)

- Week 1: CSV schema design with nullable future columns (`repeat`, `ci_lower`, `ci_upper`)
- Week 2: R7 (per-task breakdown + CSV export) + Wilson CIs on `ConfigSummary` with task-sampling-only label
- Week 3: R8 (HTML report) — costs now honest, per-task tables available for diagnostics
- Week 4: CIs and stats from Track B populate into existing report formats; warning banners removed

### Independent (any time)

- R10 (LLM enrichment) + R11 (quality_score persistence) + R12 (sandbox detection)
- R9 (tiktoken) — optional dependency, improves Copilot token accuracy

## Converge Debate Results

Three positions debated sequencing over 2 rounds:

| Position               | Core Argument                                                                       | Strongest Contribution                                                                    |
| ---------------------- | ----------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------- |
| Foundation-first       | Don't ship reports on untrustworthy data                                            | Wilson CI critique: single-run CIs measure task-set variance, not measurement reliability |
| Quick-wins pragmatist  | Ship independent fixes immediately; stop active harm                                | "Independent means ship independently" — broke false serialization                        |
| Enterprise-credibility | Reports are the product surface; transparent imperfection beats hidden imperfection | Reports as diagnostic infrastructure — per-task tables validate isolation correctness     |

**Resolved consensus:**

- R5/R2/R6/R14 ship immediately (all agreed)
- Parallel tracks, not sequential waterfall (all agreed)
- SKILL.md update is urgent (all agreed)
- HTML report comes after cost fixes (enterprise-credibility conceded)
- Provenance tagging is a cross-cutting design principle (all adopted)

**Conditionally resolved:**

- Wilson CIs ship with explicit task-sampling-only labeling (compromise between enterprise-credibility and foundation-first)
- CSV schema designed upfront with nullable future columns (foundation-first's stability concern + quick-wins' urgency)

## Open Questions

1. Do Claude Code's `~/.claude/` session files conflict across parallel worktree instances?
2. What is the actual error margin of the 4-chars/token heuristic on real Copilot output?
3. Should reports auto-detect binary vs continuous scores for test selection (less ZFC-compliant), or require user declaration?
4. Should codeprobe support a `cost_override` field for subscription agents to enable per-token comparisons?
5. The real mining yield bottleneck may be test-command mapping (only Python/Go/JS/TS supported), not description quality — should R10 enrichment wait until more languages are supported?

## Research Provenance

Five independent research agents explored this space:

| Lens                     | Key Contribution                                                                            |
| ------------------------ | ------------------------------------------------------------------------------------------- |
| Task Isolation           | Found prompt-path coupling; proposed worktree pool pattern                                  |
| Statistical Rigor        | Identified Cliff's delta over Cohen's d for binary data; McNemar's test; 5×20 sample sizing |
| Enterprise Reporting     | Revealed SKILL.md documentation gap; CSV as highest-ROI feature                             |
| Token Accuracy           | Uncovered Copilot costs using wrong pricing model; dead `cost_source="estimated"` value     |
| Quality Gates & Headless | Found `auto` mode suffices for most eval; real bottleneck is test-command mapping           |

**Convergence**: All agents confirmed the isolation → repeats → reports dependency chain and that the codebase is architecturally ready for these improvements.

**Key divergence**: Cohen's d vs Cliff's delta (resolved: use Cliff's delta for binary, Cohen's d for continuous). Priority of `--dangerously-skip-permissions` (resolved: lower than initially assumed since `auto` mode covers most cases).

## Premortem Risk Summary

Full premortem analysis: `premortem_codeprobe_data_trust_and_enterprise_reporting.md`

**All 5 failure lenses rated Critical/High** — this is unusual and indicates the project is at a critical juncture where the planned work addresses real problems but the implementation approach has significant gaps.

**Top 3 cross-cutting risks:**

1. **Agent session state isolation** (3 lenses) — worktrees isolate filesystem but not `~/.claude/`, env vars, or MCP temp files
2. **Silent degradation** (3 lenses) — broad except clauses produce plausible-looking garbage instead of failing loudly
3. **Report misinterpretation** (2 lenses) — statistically correct reports lead non-statisticians to wrong conclusions

**4 P0 mitigations (must complete before shipping R1):**

1. Restrict agent subprocess env via whitelist (~5 lines)
2. Resolve Open Question #1 — test parallel Claude sessions for `~/.claude/` conflicts
3. Add `isolate_session()` to AgentAdapter protocol
4. Global concurrency semaphore for total active subprocesses

**1 promotion:** R12 (sandbox detection) promoted from Nice-to-Have to prerequisite for `dangerously_skip` permission mode.
