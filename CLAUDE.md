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

## Key Constraints

- ALL adapters must extract token/cost data — never just document a shortcoming
- Validate-or-die on all data boundaries (premortem finding)
- Partial results preserved with error field, never crash silently
- Score failures as "incorrect" rather than dropping them
