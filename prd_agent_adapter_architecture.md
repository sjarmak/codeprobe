# PRD: Agent Adapter Architecture for codeprobe v0.2+

## Problem Statement

codeprobe needs to support TWO execution modes for evaluating AI coding agents:

1. **Headless mode** — run agents (Claude Code, Copilot, Codex) externally via subprocess/API, capture output/tokens/cost, return structured results. The agent is the _subject_ being evaluated.
2. **Interactive mode** — instrument the _current_ agent session from inside (e.g., a Claude Code session running codeprobe skills). Capture tokens consumed, cost accrued, tools used, time spent in real-time. The agent is the _tool_ helping the user build/run evals.

The current adapter protocol is CLI-shaped (`find_binary`, `build_command`), making API adapters awkward and interactive mode impossible. Each agent returns different output formats and uses incompatible pricing models. The framework needs a clean architecture that supports both modes, sharing maximum infrastructure while respecting their fundamentally different execution models.

## Goals

- **G1**: Narrow the `AgentAdapter` Protocol to the minimal headless contract (`name`, `preflight`, `run`)
- **G2**: Support both CLI subprocess and API SDK execution models cleanly
- **G3**: Parse and normalize agent-specific output formats into AgentOutput with token/cost fields
- **G4**: Handle adapter failures gracefully — preserve partial results, never silently corrupt data
- **G5**: Make adding a new agent adapter a < 1 hour task (one file + entry point + fixtures)
- **G6**: Report cost data honestly — show what's available, annotate what's missing, never fabricate
- **G7**: Separate telemetry extraction from execution so interactive mode can reuse collectors
- **G8**: Design for incremental delivery — headless ships first, interactive layers on without refactoring

## Non-Goals

- Centralized LiteLLM pricing table (Phase 3+, when 3+ API adapters exist)
- Event-sourced AgentOutput (converge debate resolved: premature for v0.2, revisit Phase 4+)
- Universal "Compute Unit" normalization across cost models
- Container-packaged adapters (OCI distribution)
- Async adapter protocol surface (adapters manage async internally)

## Requirements

### Must-Have

1. **Slim Protocol**: `AgentAdapter` Protocol exposes only `name: str`, `preflight(config) -> list[str]`, `run(prompt, config) -> AgentOutput`. Remove `find_binary()` and `build_command()` from Protocol; keep them in `BaseAdapter` (CLI base class only).

2. **Claude JSON parsing**: `ClaudeAdapter.run()` parses `--output-format json` stdout to extract `result`, `usage.input_tokens`, `usage.output_tokens`, `usage.cache_read_input_tokens`, `total_cost_usd`. Set `cost_model="per_token"`. On parse failure: log warning, set token/cost fields to `None`, set `error` field with description.

3. **Codex API adapter stub**: `CodexAdapter` implements Protocol directly (no BaseAdapter inheritance). Uses `openai` SDK to call API. Parses `usage.prompt_tokens`, `usage.completion_tokens`. Calculates `cost_usd` from token counts x published pricing. `openai` is optional dependency (`pip install codeprobe[codex]`). Registered via pyproject.toml entry point.

4. **Copilot token/cost extraction**: `CopilotAdapter` MUST extract token usage data, not just document a shortcoming. Research found two viable extraction paths: (a) **Process log parsing** — parse `~/.copilot/logs/process-*.log` for `CompactionProcessor` entries containing per-message token deltas (tokentop pattern), with `content.length / 4` heuristic fallback; (b) **PTY snooping** — attach subprocess to a pseudo-terminal via `pty.openpty()` to capture ephemeral debug/log output before process exit. Implementation should try log parsing first (less invasive), fall back to heuristic estimation. Set `cost_model="subscription"` and `cost_source="log_parsed"` or `"estimated"` accordingly. Token counts enable meaningful resource comparison even when per-invocation dollar cost is unavailable. Research phase: capture raw Copilot CLI output under various flags (`--debug`, `--verbose`, `--output-format json` if supported) to catalog all available telemetry before committing to an extraction strategy.

5. **Partial result preservation**: `BaseAdapter.run()` catches `subprocess.TimeoutExpired` (preserving `e.stdout`, `e.stderr`) and `FileNotFoundError`. Returns `AgentOutput` with available data and `error` field set.

6. **Error field on AgentOutput**: Add `error: str | None = None` to the frozen dataclass. Adapters populate this on parse failures, timeouts, or execution errors instead of crashing.

7. **parse_output hook**: `BaseAdapter` gains a `parse_output(result: subprocess.CompletedProcess, duration: float) -> AgentOutput` method. Default implementation returns raw stdout/stderr/exit_code. `ClaudeAdapter` overrides to parse JSON envelope. Future CLI adapters override as needed.

### Should-Have

8. **AdapterError hierarchy**: `AdapterError` base with `AdapterSetupError` (binary not found, auth failure — don't retry), `AdapterExecutionError` (timeout, crash — retry with backoff), `AdapterParseError` (got output but can't parse — return partial). Organized by caller action, not internal cause.

9. **cost_source field**: Add `cost_source: str = "unavailable"` to AgentOutput. Values: `"api_reported"`, `"calculated"`, `"estimated"`, `"unavailable"`. Downstream analysis uses this to decide confidence in cost data.

10. **Contract test suite**: Parametrized pytest suite (`tests/test_adapter_contract.py`) that runs against every registered adapter class. Validates: name is non-empty string, preflight returns list, run returns AgentOutput, fields are correct types. Formalize FakeAdapter as reference implementation.

11. **JSON test fixtures**: `tests/fixtures/claude_normal.json`, `claude_partial.json`, `claude_malformed.json`, `claude_timeout.json`. Loaded via parametrized pytest fixture for adapter parsing tests.

12. **Fix pareto.py bug**: `cost_usd or 0.0` conflates missing cost with zero cost. Exclude agents with `cost_model != "per_token"` from cost-based Pareto analysis.

13. **Protocol validation in registry**: Add `isinstance(instance, AgentAdapter)` check in `registry.resolve()` after constructing adapter. Provides shallow guard (names only, not signatures) — contract test suite is the real safety net.

### Nice-to-Have

14. **APIBaseAdapter**: Shared base class for API adapters with retry logic, timeout wrapping, SDK client lifecycle. Justified when 2+ API adapters exist. For now, CodexAdapter implements Protocol directly.

15. **Value Score and CPST metrics**: `pass_rate / total_cost_usd` (Value Score) and `total_cost_usd / passed_tasks` (Cost Per Successful Task) as first-class derived metrics in the analysis module. Only computed for agents with `cost_source != "unavailable"`.

16. **Two-section report layout**: "Performance Ranking" (all agents, quality + speed) and "Cost-Efficiency Analysis" (only per-token agents with cost data). Prevents false comparisons.

17. **Adapter capability declaration**: Optional `capabilities() -> frozenset[str]` method on Protocol. Values like `"cost_tracking"`, `"token_reporting"`, `"mcp_tools"`. Informs downstream what data to expect.

## Design Considerations

### Key Tensions

**Inheritance vs Composition for base classes**: Research found that the adapter itself IS the strategy — composition (Executor pattern) adds indirection without benefit. Keep `BaseAdapter` as CLI convenience class, let API adapters implement Protocol directly. Revisit APIBaseAdapter when the second API adapter arrives.

**stdout naming**: `AgentOutput.stdout` is semantically wrong for API adapters (no subprocess stdout). Renaming to `output` is cleaner but creates churn. Decision: keep `stdout` in v0.2 (alpha, few call sites), document the semantic meaning as "primary output text," rename in v0.3 if it causes confusion.

**Strict vs lenient validation**: The premortem says "validate-or-die on data boundaries." Resolution: structural validation (cost_model enum, required fields) stays strict in `__post_init__`. Sanity checks (plausible token ranges, non-negative costs) belong in a separate `validate_sanity()` function that returns warnings, not exceptions.

**Scoring failures vs dropping them**: Inspect AI scores timed-out/errored runs as "incorrect" rather than excluding them. This is statistically sound for benchmarking — excluding failures biases toward easier tasks. codeprobe should adopt this pattern.

### Architecture (Converge Result: "Adapter + Collector" Hybrid)

The converge debate (4 advocates: Unified Protocol, Dual Protocol, Observer Pattern, Layered Architecture) produced consensus on a hybrid architecture:

**Key insight**: Execution and telemetry are separate concerns. Headless mode needs both (run agent + extract usage). Interactive mode needs only telemetry (the agent session already exists). The `TelemetryCollector` is the shared seam.

```
HEADLESS MODE                          INTERACTIVE MODE

AgentAdapter Protocol                  SessionCollector Protocol
├── name: str                          ├── name: str
├── preflight(config) -> list[str]     ├── preflight(config) -> list[str]
└── run(prompt, config) -> AgentOutput ├── start_capture(config) -> None
         │                             ├── snapshot() -> AgentOutput
         │ internally composes         └── stop_capture() -> AgentOutput
         ▼
TelemetryCollector Protocol  ◄─── shared between both modes
├── collect(raw_output) -> UsageData   (stateless, for headless)
└── (Phase 2) start/snapshot/stop      (stateful, for interactive)

         │
         ▼ both produce
    AgentOutput (frozen dataclass) → scoring → analysis → reporting
```

**Headless flow**: `adapter.run()` → subprocess/API → `collector.collect(raw)` → populate AgentOutput

**Interactive flow**: `session_collector.start_capture()` → user works → `snapshot()` for real-time → `stop_capture()` → final AgentOutput

**Concrete adapters (Phase 0)**:

```
BaseAdapter (CLI base)     [direct Protocol impl]
    │         \                    \
Claude     Copilot              Codex
  │           │                    │
JsonStdout  LogFile           ApiResponse
Collector   Collector          Collector
```

**Incremental phasing**:

- Phase 0 (v0.2): `parse_output()` hook on BaseAdapter IS the collector informally. Ships now.
- Phase 1 (v0.3): Extract formal `TelemetryCollector` protocol from `parse_output()` pattern.
- Phase 2 (v0.4): Add `SessionCollector` protocol for interactive mode. Reuses collectors.

### Converge Debate Resolution

| Tension                              | Resolution                                            | Decisive Argument                                                         |
| ------------------------------------ | ----------------------------------------------------- | ------------------------------------------------------------------------- |
| One protocol vs two                  | Two protocols sharing AgentOutput                     | "`run(prompt)` when there's no prompt is encoding a lie" (3/4 agreed)     |
| Events vs flat dataclass             | Flat dataclass for v0.2, events internal later        | "Event sourcing solves a scaling problem we don't have yet" (3/4 agreed)  |
| Telemetry inside vs outside adapters | Separate TelemetryCollector, composed into adapters   | "Execution failure vs parse failure are independent domains" (4/4 agreed) |
| Where interactive mode lives         | SessionCollector protocol, uses collectors standalone | "Interactive mode IS pure telemetry — no adapter involved" (4/4 agreed)   |

## Open Questions

1. **Copilot extraction stability**: Log format for `~/.copilot/logs/process-*.log` is internal and may change across Copilot CLI versions. Mitigation: version-detect Copilot CLI, maintain parser per known version, fall back to heuristic (`content.length / 4`) for unknown versions. Monitor GitHub copilot-cli releases for format changes.

2. **Multi-model token breakdown**: Agents like Aider use main/weak/editor models internally. Should AgentOutput support per-sub-model usage? **Recommendation**: Not in v0.2. Add `model_breakdown: dict | None` field in Phase 3 if needed.

3. **Concurrent rate limits**: Running N Claude tasks in parallel shares one API key. Need semaphore/token-bucket at runner level? **Recommendation**: Phase 3 concern for Persona B (50+ tasks). Note in executor docs.

4. **Protocol versioning**: If Protocol gains methods in v0.3, third-party plugins break. **Recommendation**: Semantic versioning of the codeprobe package is sufficient. Protocol is alpha, document that it may change.

5. **Interactive mode session telemetry sources**: What data is available from inside a Claude Code session? (`CLAUDE_CODE_SESSION_ID`, `~/.claude/projects/*/statsig/`, API usage files?) What about Copilot workspace sessions? Codex sandbox sessions? **Recommendation**: Research during Phase 1, catalog telemetry sources per agent before implementing SessionCollector.

6. **SessionCollector lifecycle**: Should `snapshot()` be callable multiple times for real-time dashboards, or is stop_capture() the only way to get data? **Recommendation**: Support both — `snapshot()` for live feedback, `stop_capture()` for final results.

## Research Provenance

### Lenses and Key Contributions

| Lens                   | Key Contribution                                                                                                                 |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| **Prior Art**          | Terminal-Bench validates dual-base-class pattern; industry returns 0 tokens for CLI agents; OpenAI Evals confirms Protocol > ABC |
| **First-Principles**   | Executor only calls `run()` — Protocol surface is 75% dead; composition would make things worse                                  |
| **Failure Modes**      | `TimeoutExpired` preserves partial output; 3-level error hierarchy by caller action; Inspect AI scores failures                  |
| **Cost Normalization** | Copilot tracks tokens internally; `pareto.py` has zero-conflation bug; Value Score and CPST are proven metrics                   |
| **Extensibility**      | `runtime_checkable` only checks names not signatures; contract test suite > isinstance; FakeAdapter is informal reference impl   |

### Convergence (high confidence)

- Narrow Protocol to 3 methods (3/5 agents)
- Split CLI/API base classes (3/5 agents)
- Return partial results, don't crash (2/5 agents + premortem)
- Show what you have, annotate what's missing for cost (2/5 agents)

### Divergence (resolved)

- LiteLLM dependency: deferred (start with direct SDK)
- stdout renaming: deferred (document semantic meaning)
- Pricing table: deferred (adapters compute own cost for now)

### Converge Debate (4 advocates, 2 rounds)

| Advocate                 | Position                                  | Key Move in Round 2                                       |
| ------------------------ | ----------------------------------------- | --------------------------------------------------------- |
| **Unified Protocol**     | One AgentAdapter for both modes           | Adopted TelemetryCollector as composable internal         |
| **Dual Protocol**        | Separate AgentAdapter + SessionInstrument | Adopted TelemetryCollector vocabulary                     |
| **Observer Pattern**     | Event stream as primary abstraction       | Merged with Layered; dropped event sourcing for v0.2      |
| **Layered Architecture** | Separate execution from telemetry         | Adopted Dual's lifecycle vocabulary (start/snapshot/stop) |

**Consensus**: TelemetryCollector as shared seam (4/4). Two protocols for two modes (3/4). Events premature (3/4). AgentOutput as shared downstream currency (4/4).

### Brainstorm Highlights (30 ideas generated, top picks)

- **#3 Canonical Output Normalizer**: parse_output as strategy function per adapter — adopted as `parse_output()` hook
- **#5 Partial-Result Preservation via Sentinel**: PartialOutput dataclass with completeness score (consider for Phase 3)
- **#8 Preflight as Structured Health-Check Report**: PreflightReport dataclass (consider for should-have)
- **#13 FakeAdapter Factory via Hypothesis**: Property-based testing of adapter invariants (consider for Phase 3)
- **#16 Deterministic Replay from Recorded Sessions**: JSONL cassette record/replay for CI (consider for Phase 3)
- **#22 Copilot Token Extraction via PTY Snooping**: Attach subprocess to PTY for ephemeral log capture — viable extraction path
- **#24 Protocol Conformance Test Plugin**: Ship as pytest plugin for third-party adapter authors (consider for Phase 4)
- **#26 AgentOutput as Event Log**: Event-sourced output — debated in converge, deferred to Phase 4+
