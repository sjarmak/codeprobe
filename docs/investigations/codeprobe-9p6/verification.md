# codeprobe-9p6 — verification verdict (mol-focus-review gate)

**Bead:** codeprobe-9p6 — `[obs]` Persist claude stream-json MCP init manifest per trial
(zero-inference tool-availability proof)
**Branch:** `codeprobe-9p6-mcp-init-manifest`
**Commits:**
- `f81fbd1` feat(obs): persist per-trial MCP init manifest — implementation + tests
- `4baed31` fix(obs): reject bare system event in mcp_init parser — review-gate hardening

**Verdict:** PASS (branch-ready — NOT pushed, NOT merged; mayor publishes after Stephanie approval per bead constraint).
**Reviewer:** codeprobe-worker-gc-371995 (mol-focus-review formula, `fny47`) + independent
`code-reviewer` subagent (`ac33305e615f0edb3`).

## What shipped

The Claude CLI runs with `--output-format stream-json --verbose`, which emits a
`type:"system"`/`subtype:"init"` event before the first turn listing the attached
`mcp_servers` (each with a `status`) and the offered `tools` (built-in +
`mcp__<server>__<tool>`). Previously the adapter consumed that stream live and persisted
only the final assistant text, so no on-disk record proved which tools were available per
arm. The change captures it:

- `McpInitManifest` / `McpServerStatus` (typed, frozen) in `adapters/protocol.py`;
  `AgentOutput.mcp_init` additive field.
- `parse_mcp_init_manifest()` in `adapters/telemetry.py`, wired into
  `ClaudeAdapter.parse_output`. A failed attach is an explicit failed-status record; an
  absent init event is `captured=False` — never a silent drop.
- `CompletedTask.mcp_init` (plain dict, checkpoint-`asdict()`-safe).
- `executor._save_task_artifacts` writes `mcp_init.json` alongside `agent_output.txt`
  whenever a manifest was captured.

## Acceptance criteria — evidence

| ID | Criterion | Result | Test |
|----|-----------|--------|------|
| A1 | Artifact lists the `mcp__<server>__*` tools offered | PASS | `test_mcp_tools_property_filters_builtins`, `test_parses_offered_tools_and_servers` |
| A2 | Failed/absent attach recorded explicitly, never dropped | PASS | `test_failed_attach_is_recorded_not_dropped`, `test_no_init_event_is_captured_false_not_none`, `test_single_envelope_json_yields_uncaptured` |
| A3 | Narrow arm shows nav/search SG tools present, read/browse absent; full arm shows both | PASS | `test_narrow_vs_full_surface` |
| A4 | No scoring/reward change — additive telemetry only | PASS | grep of diff: zero `reward`/`scorer`/`ScoreResult` touch; 14 scorer-honesty lint gates green |

## Checks run

- `tests/adapters/test_mcp_init_manifest.py` + `test_adapters.py` + `test_checkpoint.py`
  + `test_executor.py` + `tests/lint/` — **281 passed**
  (run with `PYTHONPATH=<worktree>/src`, since the editable install points at the main worktree).
- ZFC: parser is pure mechanical extraction (JSON parse, fixed protocol-literal checks,
  `startswith("mcp__")` filter) — no semantic judgment, no thresholds.
- Security: `mcp_init.json` carries only `captured` / `offered_tools` / `mcp_tools` /
  `mcp_servers` (name+status) / `failed_servers` — structural metadata only, no secrets.

## Review-gate finding (fixed)

The independent reviewer flagged that `parse_mcp_init_manifest` matched any bare
`{"type":"system"}` event — returning `captured=True` with empty tools and shadowing a
real init event later in the stream. Fixed in `4baed31` by requiring at least one surface
key (`tools` or `mcp_servers`) before matching, plus a regression test
(`test_bare_system_event_does_not_shadow_real_init`).

## Non-blocking follow-ups (not addressed here)

- `adapters/_base.py` `run()` timeout-recovery branch reconstructs `AgentOutput` manually
  and does not carry `mcp_init` (consistent with the existing drop of `num_turns`,
  `result_subtype`, `tool_use_by_name` there). On a per-task timeout the manifest is lost
  even though the init event appears at stream start. Telemetry-only; out of scope for the
  stated acceptance criteria (completed trials). Candidate follow-up if timeout-trial
  surface proof is wanted.
- `failed_servers` treats any `status != "connected"` as failed (incl. `"pending"`); the
  raw verbatim status is always preserved in `mcp_servers`, so no information is lost.

## Lifecycle note

Work is **branch-ready** per the bead's Constraints ("Do not push to public; branch +
HALT at branch-ready — mayor publishes after Stephanie approval"). This bead is therefore
left for the mayor to publish/close rather than self-closed by the worker. A double-dispatch
was observed during execution (a second session implemented + committed `f81fbd1` in this
shared worktree); flagged to the mayor.
