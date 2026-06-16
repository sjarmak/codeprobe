# codeprobe-1gg — verification verdict (mol-focus-review gate)

**Bead:** codeprobe-1gg — `[analysis/validity]` Tool-surface utilization validator
**Branch:** `codeprobe-1gg-tool-surface-audit`
**Commit:** `a88aeaa` feat(validity): tool-surface utilization audit
**Verdict:** PASS (branch-ready — NOT pushed, NOT merged; close with evidence.* after mayor merge, per bead).
**Reviewer:** codeprobe-worker-gc-371995 (mol-focus-review formula, `f8ome`) + independent
`code-reviewer` subagent (`a6da77f22e41f9915`).

## What shipped

Turns the manual 9tk validity audit into a mechanism: flag arms where the agent abandoned
an ENABLED tool surface (zero calls into it). `with-sg-narrow` looked equal to `local-only`
only because the agent made zero Sourcegraph calls — an INVALID comparison that previously
read as a null result.

- `core/tool_surface_audit.py`: `ToolSurfacePolicy` / `SurfaceAuditFinding`; surfaces
  DERIVED from `ExperimentConfig` (`mcp_config.mcpServers` + `mcp__<server>__*` entries in
  `allowed_tools`), never hardcoded. Pure set intersection of declared-vs-used tools (ZFC).
- A2 honesty: a zero-call surface is `abandoned` only when the trial ran. Infra casualties
  (`status=error` / `error_category=quota`) → `reason="infra-failure"`; uncaptured usage →
  `reason="usage-not-captured"`; never conflated with a declined tool.
- `stats.ConfigSummary.abandoned_surface_count` wired through `summarize_config` /
  `summarize_completed_tasks`; `report.py` emits an INVALID-comparison warning;
  `interpret_cmd.py` surfaces it. Also fixes a pre-existing dropped `quota_error_count` in
  `summarize_config` (asymmetry vs `summarize_completed_tasks`).
- `tests/lint/test_tool_surface_policy.py`: AST lint forbidding hardcoded surface literals.

## Acceptance criteria — evidence

| ID | Criterion | Result | Notes |
|----|-----------|--------|-------|
| A1 | Zero-call enabled surface flagged `abandoned_surface_count > 0` mechanically | PASS | end-to-end smoke confirmed: with-sg count=1, local-only count=0; report warning emitted |
| A2 | Distinguishes "agent declined" (zero calls, ran) from "infra failed" (error/quota) | PASS | `_is_infra_failure` + `usage-not-captured`; `TestInfraFailureDistinction`, `TestUsageNotCaptured` |
| A3 | Surface policies config-driven; lint forbids hardcoded literals | PASS | lint verified to actually fire on a synthetic `ToolSurfacePolicy("sourcegraph", ...)` literal (2 findings); not theater |
| A4 | Tests green; no observable-score change; quota fix is a real bugfix | PASS | grep: zero scorer/reward mutation; additive `ConfigSummary` field (default 0); quota_error_count asymmetry corrected |

## Checks run

- `tests/test_tool_surface_audit.py` + `tests/lint/` + `test_analysis*` + `test_stats*`
  + `test_report*` + `test_interpret*` — **154 passed** (with `PYTHONPATH=<worktree>/src`,
  since the editable install points at the main worktree).
- ZFC: pure set intersection; the only literal is `_MCP_PREFIX="mcp__"` — the canonical
  CLI protocol prefix (structural mechanism, matching `core/mcp_policy`), not a surface policy.

## Non-blocking observation

- `_is_infra_failure` treats `error_category=="quota"` as infra failure regardless of
  `status`. A `status=="completed"` + `quota` combination (not expected under current data
  contracts) would be classed infra-failure → not abandoned — the conservative/safe
  direction. No action needed.

## Lifecycle note

Branch-ready per the bead ("NOT merged to main — close with evidence.* after mayor merge").
Left OPEN for the mayor to publish/close. Double-dispatch observed (a second session
implemented + committed `a88aeaa` in this shared worktree) — same pattern as codeprobe-9p6;
escalated to the mayor (gc-372046).
