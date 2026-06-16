# codeprobe-00e — Served arm-vs-arm comparison trace viewer

Branch: `codeprobe-00e-comparison-viewer` (off `codeprobe-3cs-run-explorer`,
worktree `/home/ds/projects/codeprobe-00e`). P2, Stephanie's 9tk verification
request. HALT at branch-ready — not pushed/merged.

## What shipped

An interactive arm-vs-arm comparison viewer, served over a port, built on the
codeprobe-3cs `run_explorer` (reusing its loader + structural validity flags),
modeled on the EnterpriseBench fable-vs-sonnet comparison viewer.

- `analysis/run_explorer.py` — loader extended to accept BOTH on-disk layouts
  (A3): the flat `per_trial.json` file (3cs) and per-arm `<arm>/results.json`
  dirs (9tk). `_normalize_completed_entry` maps a CompletedTask-shape entry
  back to the run-facing trial keys, lifting `passed` from scoring_details and
  leaving absent fields (e.g. `hit_max_turns`) ABSENT rather than fabricated.
- `analysis/comparison_viewer.py` — per-arm summary rollups (reusing
  `build_arm_summaries` for reward/cost math + structural error/zero-MCP
  counts), a per-task × arm matrix with structural delta marking, the
  self-contained comparison HTML (summary cards + side-by-side per-task table +
  per-trial drill-in), and a stdlib-`http.server` serve mode.
- `cli explore` — gained `--serve [--port 8766]` (default 8766) and now auto-
  selects the comparison view for >=2-arm runs (offline write stays default).

## Acceptance

- **A1** ✓ `codeprobe explore <9tk-run> --serve --port 8766` serves the
  arm-vs-arm comparison. The per-arm summary reproduces the 9tk headline
  EXACTLY: local-only 0.810 / $110 / 0 err, with-sg-narrow 0.781 / $94 / 0 err,
  with-sg-full 0.713 / $233 / 1 err.
- **A2** ✓ Click any task row → per-arm, per-repeat drill-in table with
  status / reward / error_category / hit_max_turns / tool_call_count /
  num_turns / cost / tokens. The structural **zero-MCP** count makes the 9tk
  finding obvious at a glance: with-sg-narrow = 30/30 zero-MCP-call trials (it
  abandoned the Sourcegraph surface — why it ≈ local-only), with-sg-full =
  0/30 (used it).
- **A3** ✓ Loader handles both the per-arm `results.json` (9tk) and
  `per_trial.json` (4cl6) layouts; missing data preserved (absent, not
  invented), no trial dropped.
- **A4** ✓ All comparison/validity signals are STRUCTURAL (status /
  error_category / hit_max_turns / tool_call_count / reward / cost / tokens /
  num_turns / zero-mcp-calls) — no semantic thresholds.
  `tests/lint/test_scorer_honesty.py` green; no reward/observable-score change.
  Tests ship in this commit.

## Design notes

- Comparison rows key on `task_id`; the 9tk run is 5 tasks × 6 repeats × 3
  arms, so each (task, arm) cell aggregates its repeats (mean reward, repeat
  count, error count, flag union) with the raw per-repeat trials carried for
  drill-in. Reward/cost math is delegated to `analysis.stats` via
  `build_arm_summaries` — not re-derived.
- Serve mode uses only `http.server` (no new deps). `make_server(html, port=0)`
  returns a bound server so tests use an ephemeral port (8766 is never
  hardcoded in a test bind); the CLI defaults to 8766.
- E501 per-file-ignore added for the HTML/JS template, matching the existing
  `run_explorer.py` / `browse.py` precedent.

## Coordination

Builds ON the unmerged `codeprobe-3cs-run-explorer` branch (related:
codeprobe-3cs). Merge order: 3cs first, then this. Not pushed/merged — mayor
publishes after Stephanie approval.
