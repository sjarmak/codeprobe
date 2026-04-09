# PRD: Improving codeprobe Tool Flow — Customizability, Ease of Use & Observability

## Problem Statement

codeprobe's eval pipeline (init → mine → run → interpret) has a functional core but a significant gap between the data the system tracks internally and what it surfaces to users. During runs, the executor tracks cumulative cost, token usage, error categories, and checkpoint timestamps — but the user sees only `task-001: PASS (5.2s)` per completion. Cost budget warnings fire to a logger that is suppressed at default log level, meaning users can hit their budget and see tasks silently stop. Configuration is fragmented: `.evalrc.yaml` is written by init but never read by run, the `experiment add-config` command lacks the MCP auto-discovery that init provides, and CLI flags cannot cleanly override experiment.json values. The `mine` command exposes 18 flags with no grouping or presets, creating a discoverability wall. Extensibility is inconsistent: agent adapters use entry_points while scorers use a hardcoded dict, and the executor has a single lifecycle hook (`on_task_complete`) that prevents both richer observability and third-party integrations.

These gaps compound into a "golden path" anti-pattern: the init wizard is polished, but any deviation from the wizard-created experiment drops users into raw CLI with no guardrails. The tool's quality is limited by its weakest touchpoint, not its best one.

## Goals & Non-Goals

### Goals

- Make run-time observability rich by default: progress, cost, ETA, pass rate — all visible during execution
- Create a clean event/hook architecture that decouples the executor from presentation and enables extensibility
- Unify the extension model: all pluggable components (adapters, scorers, reporters) discoverable via entry_points
- Fix configuration inconsistencies: eliminate dead config, enable CLI overrides, surface validation errors
- Reduce cognitive load for the mine command via flag grouping and presets

### Non-Goals

- Building a persistent web dashboard (terminal + structured JSON is sufficient for now)
- Full pluggy/stevedore dependency (lightweight callback protocol fits ZFC better)
- Rewriting the adapter protocol (it is stable and well-designed)
- Adding new adapters or scorers (this PRD is about the framework that enables them)

## Requirements

### Completed (v0.3 — merged to main)

- ~~**Typed event protocol replacing the bare callback**~~ DONE — `core/events.py` with 5 frozen dataclass events, queue-based EventDispatcher with daemon thread, BudgetChecker with independent budget enforcement. Wired into executor with backward-compatible on_task_complete support.

- ~~**Rich Live terminal dashboard during `codeprobe run`**~~ DONE — `cli/rich_display.py` with RichLiveListener. TTY detection, `--force-plain`/`--force-rich` flags, CI env var detection, thread-safe rendering. `rich>=13.7,<14` hard dependency.

- ~~**Cost budget warnings visible at default log level**~~ DONE — 80% and 100% thresholds, visible on stderr without `-v`. Both sequential and parallel modes. Thread-safe sandbox ref-counting.

- ~~**Entry_points for scorers**~~ DONE — `codeprobe.scorers` entry point group in pyproject.toml. `scoring.py` delegates to `registry.resolve_scorer()`. Built-in scorers registered through the same mechanism as adapters.

- ~~**Fix dead `.evalrc.yaml`**~~ DONE — Removed write from init. Deprecation warning when file exists. yaml_writer deprecated.

- ~~**`--preset` flag for `codeprobe mine`**~~ DONE — `--preset quick` (count=3) and `--preset mcp` (org-scale + enrichment). Explicit flags override preset values.

- ~~**MCP config discovery shared between init and add-config**~~ DONE — `core/mcp_discovery.py` shared module. Used by init and experiment add-config.

- ~~**Structured JSON event lines for CI**~~ DONE — `cli/json_display.py` with JsonLineListener. `--log-format json` emits JSON lines on stderr. `--quiet` + `--log-format json` still emits JSON.

- ~~**Flag grouping in mine help text**~~ DONE — Presets visible in help. Flag organization improved.

### Remaining (v0.4 — next prd-build)

- **Layered config resolution with CLI override precedence**
  - Acceptance: `codeprobe run . --model opus-4 --timeout 600` overrides experiment.json values without editing the file. Precedence documented: built-in defaults < user config < project config < experiment.json < CLI flags. Config resolution logged at debug level.

- **Ctrl+C integration test**
  - Acceptance: Test spawns a real subprocess running codeprobe, sends SIGINT after 2 seconds, verifies partial results are written to disk. No traceback spam. Works on Python 3.11+.

- **Adapter lazy imports + output contract tests**
  - Acceptance: A missing CLI tool (e.g., copilot-cli) does not crash codeprobe at import time. Each adapter's test suite asserts non-None values for cost fields in fixture output.

- **`codeprobe preambles list` command**
  - Acceptance: Shows available preambles at each search path level (built-in, user, project, task) with template variable documentation.

- **Prompt dry-run (`codeprobe run --show-prompt`)**
  - Acceptance: Prints the fully-resolved prompt (instruction + preambles + template variables) for the first task without calling any agent. Useful for debugging prompt composition.

- **`codeprobe doctor` command**
  - Acceptance: Checks for installed agents (claude, copilot, codex), API keys, git status, MCP server availability. Reports pass/fail per check with fix suggestions.

- **User-defined mine profiles**
  - Acceptance: Users can save custom flag combinations as named profiles in a config file. `codeprobe mine --profile my-setup` loads saved flags. Extends the preset system based on real usage patterns.

## Design Considerations

**Event system granularity:** The event protocol should have ~5-7 event types, not 70 (pytest's count). ZFC means the executor should emit mechanical lifecycle events, not semantic ones. Events carry raw data; interpretation is the consumer's job.

**Rich as dependency:** Rich is ~3MB, pure Python, no native extensions. Recommend hard dependency (not optional extra) since observability is a core feature, not a plugin. Use `rich.console.Console(stderr=True)` so live display goes to stderr while machine-readable output stays on stdout.

**Thread safety for parallel runs:** Rich's `Live` display is thread-safe, but this must be validated with codeprobe's specific `ThreadPoolExecutor` usage. The event listener model naturally handles this: each thread emits events, one listener (the Rich display) consumes them from a single render loop.

**Config format decision:** The codebase already uses TOML (`task.toml`) and YAML (`.evalrc.yaml`) and JSON (`experiment.json`). Standardizing on one format is desirable but out of scope for this PRD. The immediate fix is making `.evalrc.yaml` either functional or removed.

**ZFC compliance:** All recommendations are ZFC-compliant. Events are mechanical lifecycle signals (IO/state), not semantic judgments. Config resolution is structural validation. Rich display is pure presentation. Entry_points are mechanical discovery. No heuristics introduced.

## Open Questions (Post-Debate)

1. ~~Should `.evalrc.yaml` be revived or killed?~~ **RESOLVED: Kill it.** Unanimous after debate. A file written but never read is a bug. Remove the write from init. If layered config demand materializes, design it fresh with real requirements.

2. ~~Should Rich be a hard dependency or optional?~~ **RESOLVED: Hard dependency.** Observability is a core feature. An optional dep means two code paths, one perpetually undertested. Rich is ~3MB, pure Python, no native extensions. Use `Console(stderr=True)` so live display goes to stderr.

3. ~~Event system: typed union vs callback dataclass?~~ **RESOLVED: Callbacks with typed event payloads.** The compromise that all three positions converged on: `ExecutorHooks` as a callback dataclass (simple surface area), but each callback receives a typed frozen dataclass event as its argument — e.g., `on_task_scored(event: TaskScored)` not `on_task_scored(task_id, score, duration)`. This gives minimal implementation cost with clean future migration to a full event bus if needed.

4. How should adapter-specific configuration be validated? The `extra: dict` field currently has no schema. Deferred — no immediate user pain. Options: (a) each adapter declares a schema in `preflight()`, (b) leave untyped until a real misconfiguration bug occurs.

5. Is there demand for cross-run trend tracking? Deferred — requires persistent store beyond per-experiment checkpoint.db. No current user request.

## Convergence Debate Results

A structured 3-position debate refined the PRD. Positions: **Simplicity Maximalist**, **Architecture-First**, **User-Impact-First**.

### Resolved Points (Consensus)

| Decision | Outcome | Decisive Argument |
|----------|---------|-------------------|
| Kill `.evalrc.yaml` | Remove write from init | 3-0 after Architect conceded: "a file written but never read is a bug" |
| Fix cost budget warning | Ship immediately as P0 bugfix | Unanimous: users losing money silently; 3-line fix, no abstraction needed |
| Event payloads as typed dataclasses | Callbacks receive frozen event objects | Bridges simplicity (flat callbacks) with architecture (typed contracts) |
| Rich as hard dependency | Include in core install | 2-1 (Simplicity conceded optionality adds untested code paths) |
| Entry_points for scorers | Defer to v0.4+ | 2-1 (Architect overruled: zero third-party scorers exist today) |

### Refined Shipping Sequence

All three positions converged on a triage-first approach:

**Phase 0 — Bugfixes (ship immediately, point release):**
1. Fix cost budget warning — surface at default log level, clear stop message
2. Kill `.evalrc.yaml` — remove write from `init_cmd.py`

**Phase 1 — Event system + Rich dashboard (v0.3, ~2-3 days):**
1. Typed event dataclasses (`RunStarted`, `TaskStarted`, `TaskScored`, `BudgetWarning`, `RunFinished`)
2. `ExecutorHooks` callback dataclass accepting typed events
3. Plain text consumer (replaces current `_on_task_complete`, zero UX change)
4. Rich Live consumer — progress bar, pass rate, cost, ETA on stderr

**Phase 2 — Quick wins (v0.3, parallel with Phase 1):**
1. Mine presets (`--preset mcp`, `--preset quick`)
2. MCP config discovery shared between init and add-config
3. Flag grouping in `codeprobe mine --help`

**Phase 3 — Future (v0.4+, driven by demand):**
1. Entry_points for scorers (when a third-party scorer appears)
2. Structured JSON event lines for CI
3. Layered config resolution (if users request project-level defaults)
4. `codeprobe preambles list` and `--show-prompt`

### Strongest Arguments Preserved

- **Simplicity**: "YAGNI on event filtering, middleware, and replay. Five frozen dataclasses plus a listener Protocol is a day's work — don't build a framework."
- **Architect**: "Skipping the event protocol and wiring Rich directly into the run loop creates tight coupling. Every new output format would require touching pipeline code."
- **User-Impact**: "Ship Rich with callbacks NOW; refactor later" was challenged by Architect's observation that "the refactoring never happens" — the callback-wired dashboard becomes load-bearing. This was the decisive argument for events-before-Rich.

### Debate Highlights

- **Simplicity** made the strongest case for killing `.evalrc.yaml`, convincing Architect to concede. Also proposed the key compromise: accept typed events IF implemented without registry/middleware/bus.
- **Architect** won the sequencing argument: events-then-Rich costs ~1 extra day but avoids a throwaway callback layer. Conceded on `.evalrc.yaml` and entry_points timing.
- **User-Impact** established the triage framing (bugs/quick-wins/architecture) that all positions adopted. Identified the cost budget warning as a P0 bug that reframes the entire prioritization.

## Research Provenance

Three independent research agents contributed from uncorrelated perspectives:

- **UX & Workflow Design** — identified the golden-path anti-pattern, mine's 18-flag discoverability wall, the data-exists-but-isn't-shown gap, and the MCP discovery inconsistency
- **Observability & Real-Time Feedback** — identified the invisible budget warnings (usability bug), CheckpointStore as untapped time-series source, benchmarked against Inspect AI/Braintrust, and proposed the typed event protocol
- **Extensibility & Configuration Architecture** — identified dead `.evalrc.yaml`, hardcoded scorer registry despite existing entry_points pattern, and proposed lightweight ExecutorHooks over pluggy

**Key convergence:** All three independently identified that the executor's internal data is invisible to users, and all three recommended replacing the single callback with a richer event/hook system. This is the highest-confidence finding.

**Key divergence:** Event system architecture (typed union vs. callback dataclass) — resolved through debate: callbacks with typed event payloads as the compromise. Implementation sequence refined to: bugfixes first, then events + Rich together, then quick wins in parallel.

## Premortem Risk Analysis

A 5-lens premortem identified critical risks. Full analysis: `premortem_improving_tool_flow_customizability_ease_of_use_observability.md`

### Top Risks

| # | Risk | Score | Root Cause |
|---|---|---|---|
| 1 | Display stall cascades to budget bypass | 12 (Critical/High) | Display coupled to event dispatch path |
| 2 | Rich breaking change crashes all runs | 12 (Critical/High) | Unbounded dependency + no integration tests |
| 3 | Synchronous events serialize parallel work | 6 (High/Medium) | Inline callbacks in worker threads |
| 4 | CI adoption blocked by missing JSON output | 6 (High/Medium) | Structured events deferred to v0.4+ |
| 5 | Scorer extensibility gap causes forking | 6 (High/Medium) | Entry_points deferred to v0.4+ |

### Design Modifications (from Premortem)

**CRITICAL — Must address before shipping v0.3:**

1. **Queue-based event dispatch** instead of inline synchronous callbacks. Workers push to `queue.Queue`; a single dispatcher thread fans out to consumers. Budget checker gets independent queue tap. This modifies the convergence debate's "ExecutorHooks" design — the external API stays the same but internal dispatch changes.

2. **Decouple budget checker from display.** Budget enforcement must never be blocked by a display stall, Ctrl+C handler, or --quiet flag. Explicit test: `--quiet` + low `--max-cost` must still trigger the budget stop.

3. **Pin Rich `>=13,<15`** with upper bound. Add Rich Live smoke test to CI.

**HIGH — Promote from v0.4+ to v0.3:**

4. **Ship JsonLineConsumer** alongside RichConsumer. One JSON line per event on stderr via `--log-format json`. Three failure lenses identified this as a CI adoption blocker, not a nice-to-have.

5. **Scorer entry_points interface** — register built-in scorers through it. Near-zero cost (infrastructure exists in `registry.py`), prevents forking. Reverses convergence debate's deferral decision based on premortem severity.

6. **Robust TTY detection** — check `isatty()` + `TERM` + `CI` env vars. Add `--force-plain`/`--force-rich` override. 500ms write timeout on display path with auto-degrade.

7. **Ctrl+C integration test** — spawn subprocess, send SIGINT, verify partial results on disk.

8. **.evalrc.yaml deprecation warning** — detect existing file, print migration message for 1 minor version before silent removal.

### Revised Shipping Sequence (Post-Premortem)

**Phase 0 — Bugfixes (point release):**
1. Fix cost budget warning visibility
2. Add deprecation warning for .evalrc.yaml (detect + warn, remove write in next minor)

**Phase 1 — Event system + Consumers (v0.3, ~3-4 days):**
1. Typed event dataclasses (5 types)
2. Queue-based dispatcher (not inline callbacks) — single consumer thread
3. Budget checker with independent event tap
4. Plain text consumer (default non-TTY / --quiet)
5. Rich Live consumer with robust TTY detection + write timeout + auto-degrade
6. JsonLineConsumer for CI (`--log-format json`)
7. Pin Rich `>=13,<15`
8. Ctrl+C integration test + --quiet budget enforcement test

**Phase 2 — Quick wins (v0.3, parallel):**
1. Scorer entry_points (promote from v0.4)
2. Mine presets
3. MCP discovery sharing
4. Flag grouping in mine --help
5. Adapter lazy imports + output contract tests

**Phase 3 — Future (v0.4+):**
1. Layered config resolution
2. User-defined mine profiles (replacing/extending presets based on usage data)
3. `codeprobe preambles list` and `--show-prompt`
4. `codeprobe doctor`
