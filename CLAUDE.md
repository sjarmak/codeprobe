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
5. **Reference files with context** — `~/projects/MCP-Eval-Tasks/scripts/run_experiment.py lines 178-194, look for envelope.get('usage')`
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
- `core/llm.py` — shared Claude CLI utility for model-based judgment (pure IO + mechanical parsing)
- `analysis/ranking.py` — deterministic arithmetic with explicit tiebreakers
- `adapters/` — mechanical parsing, honest about data quality via `cost_source`
- `analysis/stats.py` — arithmetic aggregation (deterministic math, not judgment)
- `assess/heuristics.py:score_repo_with_model()` — delegates scoring to Claude via fixed RUBRIC_V1; model judges quality, code does IO
- `mining/extractor.py:generate_instruction()` — delegates instruction.md generation to LLM; regex fallback only for `--no-llm` offline mode

### Known violations (tracked for refactoring)

- `mining/extractor.py:80-87` — file-count difficulty estimation (≤3 → easy, >10 → hard). A 20-file rename is "hard" while a critical 1-file security fix is "easy". Replace with model-assessed difficulty or user-provided metadata
- `assess/heuristics.py:_detect_test_frameworks()` — regex framework detection. Structural file-glob part is OK, but "does this repo have good test coverage?" is semantic — delegate to model
- `cli/mine_cmd.py:_quality_review()` — three heuristics: length+keyword check for "thin instructions" (desc < 50 chars), hardcoded 0.7 threshold for "low diversity", stub command keyword match. These are UI warnings, not scoring judgments, so lower priority for refactoring
- `mining/org_scale_families.py` — `min_hits` thresholds (3-5) are hardcoded. Structural file-counting is OK per ZFC, but the thresholds are arbitrary. Acceptable as tunable parameters
- `mining/curator_tiers.py:assign_ground_truth_tiers()` — the `use_llm=False` branch (line ~410) returns the pure mechanical heuristic tiers without any LLM call. This is a documented offline fallback mode; callers that opt in accept the ZFC trade-off. Not a drift bug — refactor would instead tighten the docstring/labeling so consumers know when they're seeing heuristic-only tiers

### Justified exceptions

- `analysis/stats.py` — arithmetic aggregation is deterministic math, not judgment
- Secret redaction regex in `scoring.py` — pattern matching for known token formats is structural, not semantic

### When to update this section

Update ZFC compliance notes when: new heuristic code is introduced, a known violation is refactored to use model calls, or a new justified exception is added. Not per-commit — only when the heuristic landscape changes.

## Release Process

1. Bump `version` in `pyproject.toml`
2. Commit: `chore: bump version to X.Y.Z`
3. Tag: `git tag vX.Y.Z`
4. Push commit and tag: `git push && git push --tags`
5. GitHub Actions (`.github/workflows/publish.yml`) runs tests on 3.11/3.12/3.13, then publishes to PyPI via twine using the `CODEPROBE` secret

## Key Constraints

- ALL adapters must extract token/cost data — never just document a shortcoming
- Validate-or-die on all data boundaries (premortem finding)
- Partial results preserved with error field, never crash silently
- Score failures as "incorrect" rather than dropping them
