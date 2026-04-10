# Changelog

## 0.3.6 (2026-04-09)

### Features

- **Tool-call count tracking** — claude adapter now parses `tool_use` content blocks and propagates `tool_call_count` through `AgentOutput` → `CompletedTask` → `results.json` for tool efficiency analysis
- **Secret redaction** — new `config/redact.py` unconditionally redacts all Authorization header values in `ExperimentConfig.__repr__()` and `experiment.json` serialization

### Fixes

- **Timeout telemetry recovery** — timed-out agent sessions now extract partial token/cost data from stdout instead of discarding all telemetry
- **MCP instruction template** — `mine --goal mcp` now embeds the actual symbol name and definition file into `instruction.md` instead of generic phrasing
- **Test detection heuristic** — broadened to recursive `**/test*/` glob patterns, fixing false negatives for repos with nested test layouts (e.g. numpy)
- **Partial score display** — scores between 0 and 1 now show their numeric value instead of misleading FAIL; summary shows mean + perfect/partial breakdown

### Refactoring

- Batch all test detection globs into a single `git ls-files` call (was up to 22 sequential subprocess calls)
- Surface `parse_output` exceptions in timeout error field instead of silently swallowing
- Derive recursive test file globs from base list to eliminate copy-paste

## 0.3.1 (2026-04-09)

### Fixes

- Remove unsupported `aider` and `openai` agent adapters from registry, entry points, and init wizard — supported agents are now `claude`, `codex`, and `copilot`

## 0.3.0 (2026-04-09)

### Features

- **Layered config resolution** — `--model`, `--timeout`, `--repeats` CLI flags override experiment.json values; precedence logged at debug level
- **`codeprobe doctor`** — environment readiness checker for agents, API keys, git status, Python version with PASS/FAIL and fix suggestions
- **`codeprobe preambles list`** — shows available preambles at built-in/user/project levels with template variables
- **`codeprobe run --show-prompt`** — prints the fully-resolved prompt without spawning an agent (debugging aid)
- **User-defined mine profiles** — `--save-profile`, `--profile`, `--list-profiles` for saving and loading custom flag combinations
- **Mine presets** — `--preset quick` (count=3) and `--preset mcp` (org-scale + MCP families + enrich)
- **Adapter lazy imports** — missing CLI tools no longer crash at import time; clear error at resolve time
- **Adapter output contract tests** — 25 fixture-based tests asserting all adapters report cost/token fields

### Observability (v0.3 backfill)

- **Typed event protocol** — `core/events.py` with 5 frozen dataclass events, queue-based EventDispatcher
- **Rich Live dashboard** — progress, pass rate, cost, ETA during `codeprobe run` (TTY auto-detected)
- **JSON event lines** — `--log-format json` emits structured events on stderr for CI
- **Cost budget warnings** — 80% and 100% thresholds visible on stderr without `-v`
- **Scorer entry_points** — `codeprobe.scorers` group in pyproject.toml; built-in scorers registered through the same mechanism as adapters
- **MCP config discovery** — shared between `init` and `experiment add-config`

### Fixes

- Kill dead `.evalrc.yaml` — removed write from init, deprecation warning when file exists
- Ctrl+C integration test — verifies SIGINT produces exit 130 with no traceback

## 0.1.7 (2026-04-05)

### Features

- Task discovery scoped to current experiment — `mine` records task IDs in `experiment.json`, `run` filters by them
- Backward compatible: old experiments without `task_ids` keep existing behavior (no filtering)

### Fixes

- Fix `run` picking up stale tasks from previous mining runs when multiple task sets coexist

## 0.1.6 (2026-04-05)

### Fixes

- Fix `__version__` out of sync with `pyproject.toml` — CLI now reports correct version
- Skip curation verification when `--no-llm` flag is set

## 0.1.5 (2026-04-04)

### Fixes

- `codeprobe run` now finds tasks at `<repo>/.codeprobe/tasks/` when they're not inside the experiment subdirectory — fixes "No tasks found" after mining

## 0.1.4 (2026-04-04)

### Features

- `codeprobe run` auto-discovers experiments inside `.codeprobe/` — no longer requires `--config` flag when there's exactly one experiment
- Shows helpful disambiguation when multiple experiments exist

## 0.1.3 (2026-04-04)

### Fixes

- Strip markdown fences from LLM JSON responses in regular task mining (extractor.py) — the previous fix in 0.1.0 only covered the org-scale path

## 0.1.2 (2026-04-04)

### Fixes

- MCP config picker now lists all server names instead of truncating with "+N more"

## 0.1.1 (2026-04-04)

### Features

- **Auto-discover MCP configs** — `codeprobe init` now scans known locations (`~/.claude/.mcp.json`, `~/.claude/mcp-configs/`, `settings.json`) and presents a numbered picker with server names instead of requiring a manual path

### Fixes

- Tilde expansion (`~`) now works in `--mcp-config` CLI flag and init wizard path prompts

## 0.1.0 (2026-04-04)

Major release adding org-scale task mining, ground-truth curation, and eval runner improvements.

### Features

- **Org-scale task mining** — mine tasks across organizational codebases with oracle verification and multi-hop dependency tracing (`codeprobe mine --org-scale`)
- **Ground-truth curation pipeline** — curate mined tasks with pluggable backends (grep, agent_search, pr_diff), tier classification (required/supplementary/context), and weighted F1 scoring (`--curate`, `--backends`, `--verify-curation`)
- **LLM tier classification** — Haiku-powered semantic tier assignment for curated files, with heuristic fallback via `--no-llm`
- **Curation verification** — LLM-based sampling to confirm curated file sets are correct (`--verify-curation`)
- **Weighted F1 scoring** — `--metric weighted_f1` in `oracle-check` weights supplementary files lower than required files
- **Multi-repo support** — scan across multiple repositories with `--repos` flag
- **New task families** — cross-repo-config-trace, platform-knowledge, migration-inventory added to org-scale mining
- **Count and boolean oracle types** — beyond file-list oracles, tasks can now use count or boolean answer verification
- **MCP delta validation** — validate MCP tool deltas against ground truth
- **Curation quality reporting** — CLI results table shows curation stats per family
- **Interactive mine workflow** — LLM instruction generation and URL support for mine sources
- **Eval sandbox mode** — eval runs default to `dangerously-skip-permissions` with sandbox signal
- **Instruction discovery variants** — family-specific instruction templates instead of generic placeholders

### Fixes

- Skip curation verification when `--no-llm` flag is set
- Reduce PRDiffBackend noise — shorten window to 3 months, cap at 200 files
- Score partial results from timed-out agents instead of dropping them
- Copy answer.txt from repo to task dir before scoring
- Normalize CLI model names; auto-detect reward_type from task metadata
- Exclude vendor/node_modules/testdata from scanner and merge layer
- Strip markdown fences from LLM JSON responses in task generation
- Filter Python stdlib from dep-trace, cap ground truth at 500 files
- Fix org-scale multi-hop ground truth explosion and dep-trace quality
- PRDiffBackend now checks content_patterns, not just globs

### Refactoring

- Split org_scale.py from 1142 to 462 lines; extract long functions into modules
- Unify `_guess_language` into `mining/_lang.py`
- Remove dead code, improve scanner efficiency, deduplicate logic

## 0.1.0a2 (2026-04-02)

Initial public alpha with core eval pipeline.

## 0.1.0a1 (2026-04-01)

First alpha release.
