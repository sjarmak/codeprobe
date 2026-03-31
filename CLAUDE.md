# codeprobe

Python eval framework for comparing AI coding agents (Claude Code, Copilot, Codex) on quality, cost, and speed.

## Beads (Task Tracking)

This project uses `bd` (beads) for task tracking. Epic: `codeprobe-ssf`.

### MANDATORY: Bead Cold-Start Rule

Every bead description MUST contain enough context that a fresh agent session can execute the work without running explore subagents. This is non-negotiable.

**Required in every bead:**

1. **Exact file paths** — `src/codeprobe/adapters/claude.py`, not "the adapter file"
2. **Line numbers or function names** — `line 43` or `parse_output()`
3. **Numbered implementation steps** — what to do, in what order
4. **Code snippets / data shapes** — JSON schemas, Protocol signatures, dataclass fields for anything non-obvious
5. **Reference files with context** — `~/MCP-Eval-Tasks/scripts/run_experiment.py lines 178-194, look for envelope.get('usage')`
6. **Acceptance criteria** — checkboxes so the agent knows when it's done
7. **Test fixture descriptions** — what test files to create and their contents
8. **Dependency context** — what prior beads changed and how it affects this work

**Validation check before closing bead creation:** "Could a fresh agent implement this by reading only the bead description, the referenced files, and the PRD?"

**Research-phase beads** (where the work IS exploration): provide a concrete checklist of commands to run, files/URLs to check, and questions to answer. Never open-ended "investigate this area."

## Architecture

See `prd_agent_adapter_architecture.md` for the full PRD with converge debate results.

Key architecture: Adapter + Collector hybrid

- `AgentAdapter` Protocol (headless): `name`, `preflight()`, `run()` → `AgentOutput`
- `SessionCollector` Protocol (interactive): `start_capture()`, `snapshot()`, `stop_capture()` → `AgentOutput`
- `TelemetryCollector` Protocol (shared): token/cost extraction, composed into both

## ZFC Compliance

This project is AI-orchestration code — ZFC applies at two levels:

1. **L2 (tooling):** codeprobe's own orchestration code must not use heuristics for semantic judgment
2. **L3 (product):** defaults and heuristics embedded in codeprobe shape how users perceive their benchmarks

### Compliant

- `core/scoring.py` — delegates pass/fail to test.sh (gold standard ZFC)
- `analysis/ranking.py` — deterministic arithmetic with explicit tiebreakers
- `adapters/` — mechanical parsing, honest about data quality via `cost_source`
- `analysis/stats.py` — arithmetic aggregation (deterministic math, not judgment)

### Known violations (tracked for refactoring)

- `assess/heuristics.py:264-354` — hardcoded repo quality scoring with unjustified weights (0.35/0.30/0.20/0.15) and cliff thresholds (49 commits → 0.7, 50 → 1.0). Replace with model call: "given these repo stats, assess benchmarking potential"
- `mining/extractor.py:80-87` — file-count difficulty estimation (≤3 → easy, >10 → hard). A 20-file rename is "hard" while a critical 1-file security fix is "easy". Replace with model-assessed difficulty or user-provided metadata
- `assess/heuristics.py:133-172` — regex framework detection. Structural file-glob part is OK, but "does this repo have good test coverage?" is semantic — delegate to model
- `assess/heuristics.py:325-332` — hardcoded recommendation strings from if-else chain. Replace with model call using structured output

### Justified exceptions

- `analysis/stats.py` — arithmetic aggregation is deterministic math, not judgment
- Secret redaction regex in `scoring.py` — pattern matching for known token formats is structural, not semantic

### When to update this section

Update ZFC compliance notes when: new heuristic code is introduced, a known violation is refactored to use model calls, or a new justified exception is added. Not per-commit — only when the heuristic landscape changes.

## Key Constraints

- ALL adapters must extract token/cost data — never just document a shortcoming
- Validate-or-die on all data boundaries (premortem finding)
- Partial results preserved with error field, never crash silently
- Score failures as "incorrect" rather than dropping them
