# codeprobe-3cs — Self-contained HTML run-data explorer

Branch: `codeprobe-3cs-run-explorer` (worktree `/home/ds/projects/codeprobe-3cs`, off `main`).
P1, direct human request (Stephanie, Slack 2026-06-15). HALT at branch-ready — not pushed.

## What shipped

A `codeprobe explore <run-dir>` command that reads a `runs/<id>/per_trial.json`
and emits one self-contained `explorer.html` (inline CSS/JS, no server, no
network) for manual validity audits — the CodeScaleBench run-comparison
explorer pattern, adapted to codeprobe's schema.

- `src/codeprobe/analysis/run_explorer.py` — loader, structural validity-flag
  computation, per-arm summary (delegating reward/cost math to
  `analysis.stats.summarize_completed_tasks`), and the inline-template HTML
  renderer.
- `cli/__init__.py` — `codeprobe explore [RUN_DIR] [--output PATH]`. Zero-arg
  picks the newest run under `./runs`; clean `PrescriptiveError` when none.
- `tests/test_run_explorer.py` — 16 tests (flags, summaries, HTML, loading).

## Acceptance

- **A1** ✓ `codeprobe explore runs/codeprobe-4cl6` (and zero-arg → newest)
  writes a single `explorer.html` that opens from disk, no server/network
  (test asserts no external `http` `src`/`href`).
- **A2** ✓ Table renders every trial in `per_trial.json` with the validity
  columns (arm, task, rep, status, reward, score, passed, error_category,
  hit_max_turns, result_subtype, num_turns, tool_call_count, cost, tokens,
  missing); JS filters by arm/status/error_category + free-text task, all
  columns sortable.
- **A3** ✓ Flags are STRUCTURAL only: `status in (error,failed)`,
  `error_category` set, `hit_max_turns`, `reward==0 & status!=completed`,
  `tool_call_count==0` (None ≠ flag), `missing`. No semantic thresholds.
  `tests/lint/test_scorer_honesty.py` green (run_explorer is not a scorer
  module and introduces no threshold literals).
- **A4** ✓ Per-arm summary header: N, mean/median reward, mean cost, and
  per-flag-family counts; arms with any flag get a `dirty` highlight.
- **A5** ✓ Verified on real `runs/codeprobe-4cl6` (11 completed + 4 error)
  and a sparse-field fixture; partial/missing fields preserved and rendered,
  never dropped.
- **A6** ✓ Fixture run dir → HTML contains expected rows/flags; full
  analysis + CLI + lint suites green (190 + 16).

## Design notes

- Reuses `summarize_completed_tasks` for the reward/cost aggregates by mapping
  per_trial keys (`reward`→`automated_score`, `token_cost_usd`→`cost_usd`,
  `task_time_seconds`→`duration_seconds`) onto `CompletedTask` — no duplicated
  stat math (per the constraint).
- Drill-in: clicking a row toggles a detail panel with the full trial JSON
  (sub_scores included). A visible link to each trial's raw `agent_output.txt`
  / transcript artifact is a documented follow-up — deferred (not stubbed) so
  the branch carries no unwired code; it lands once run dirs adopt a single
  consistent artifact layout.
- E501 per-file-ignore added for the wide HTML/JS template, matching the
  existing `snapshot/exporters/browse.py` precedent.

## Open default (per bead — note, don't build now)

v1 bakes the chosen run's data into static HTML (CSB-style, self-contained).
A live-loading variant (open the HTML, point it at any run dir) would need a
file picker + fetch, which breaks the open-from-disk-offline guarantee unless
done with a local file input. Deferred as a follow-up; not built here.
