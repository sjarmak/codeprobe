# codeprobe-3oms — MCP-comparison rerun across mixed task families

Re-evaluation of the gascity-mcp-comparison corpus, broadened from x7p3's 5
oracle-overlap tasks to a **mixed corpus of 15 tasks across 3 scorer families**:

- 5 `oracle_overlap_fbeta` (β=0.5) — symbol-reference-trace + change-scope-audit (carry-over from x7p3)
- 5 `continuous` — SDLC implementation tasks mined from gascity merge history
- 5 `oracle_checks` — hand-authored structured-rubric comprehension tasks (CSB-style)

× 2 configs (baseline, with-sourcegraph) × N=1 = 30 trials. Model: claude-sonnet-4-6.

## Files

- [`eval_writeup.md`](./eval_writeup.md) — narrative writeup with per-config + per-family tables, per-task deltas, and oc_004 case study.
- [`aggregate.json`](./aggregate.json) — `codeprobe experiment aggregate` output (config_summaries, pairwise_deltas, scorer_family_distribution).
- [`per_trial.json`](./per_trial.json) — flat array of 30 per-trial unified ScoreResult records (`config` + `task_id` keyed for filtering).
- [`per_family_summary.json`](./per_family_summary.json) — derived per-config × per-family summary + per-task delta table.

## Quick numbers (per-family pairwise delta, with-sg − baseline)

| family                  | n | baseline_mean | with_sg_mean | delta   | direction               |
|-------------------------|---|---------------|--------------|---------|-------------------------|
| oracle_overlap_fbeta    | 5 | 0.270         | 0.418        | **+0.148** | with-sg helps strongly |
| continuous (SDLC)       | 5 | 0.633         | 0.687        | **+0.054** | with-sg helps modestly |
| oracle_checks           | 5 | 1.000         | 0.929        | **−0.071** | with-sg slightly hurts |

Run-level: baseline 0.634 vs with-sourcegraph 0.678 (delta +0.044).

The single-headline +0.044 hides three different stories — that's the
demonstration this bead was designed to surface.

## Contract validation

All 30 trials emit unified ScoreResult fields (`reward`, `score`, `status`,
`scorer_family`, `sub_scores`, `diagnostics`). `diagnostics` carries the new
`input_tokens` + `output_tokens` from codeprobe-oktg.

`aggregate.json.config_summaries[*].scorer_family_distribution` shows
`{continuous: 5, oracle_checks: 5, oracle_overlap_fbeta: 5}` for both configs —
no silent fallback.

## Reproduction

Run output preserved at:
- `/home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe/runs/`
- `/home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe/reports/aggregate.json`

Prior run data is preserved at `runs.codeprobe-x7p3/` and `reports.codeprobe-x7p3/`.

To replay:

```bash
source /home/ds/projects/codeprobe/.env.local
codeprobe run /home/ds/test_repos/gascity/gascity-mcp-comparison \
  --repeats 1 --max-cost-usd 35 --force-plain
codeprobe experiment aggregate \
  /home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe
```

Trials are checkpointed; clear `runs/<config>/checkpoint.db` to force re-run.

## Cost

Total: $48.06 (15 tasks × 2 configs × N=1, claude-sonnet-4-6). Within the
bead's $30-50 soft cap. SDLC tasks dominate at ~$3-9 per trial; oracle_checks
are cheapest at ~$0.20-0.50.
