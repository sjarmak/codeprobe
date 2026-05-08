# codeprobe-x7p3 — gascity-mcp-comparison rerun under unified ScoreResult contract

Re-evaluation of the 5 gascity-mcp-comparison tasks (38223444, 6cf61fea, b826fa9d,
d9fee4ae, e5d7a4e7) × 2 configs (baseline, with-sourcegraph), N=1 repeats, under
the unified ScoreResult contract from dr-2vydrm.4 / codeprobe-ufra (commit dca177d).

## Files

- [`eval_writeup.md`](./eval_writeup.md) — narrative writeup with per-config and per-task tables, sign-flip discussion, cost-Pareto, and contract-validation evidence.
- [`aggregate.json`](./aggregate.json) — copy of the codeprobe-emitted `reports/aggregate.json` for this run; carries `config_summaries`, `pairwise_deltas`, `bias_warnings`, `quality_metrics`, and the `scorer_family_distribution` block per config.
- [`per_trial.json`](./per_trial.json) — flat array of 10 per-trial scoring records (full unified ScoreResult shape) with `config` and `task_id` keys for easy filtering.

## Quick numbers

| metric                    | baseline | with-sourcegraph | delta   |
|---------------------------|---------:|-----------------:|--------:|
| mean_reward (fbeta β=0.5) |    0.337 |            0.543 | +0.206  |
| mean_precision            |    0.334 |            0.571 | +0.237  |
| mean_recall               |    0.833 |            0.667 | −0.167  |
| mean_f1                   |    0.364 |            0.537 | +0.173  |
| total_cost_usd            |    7.50  |            8.74  | +1.24   |
| score_per_dollar          |    0.225 |            0.311 | +0.086  |

Wins (with-sg − baseline per task): +0.302, −0.061, 0, −0.022, +0.808.
Cohen's d (paired): 0.561.

## Contract validation

10/10 trials emitted `scoring.json` with all unified-contract fields:
`reward`, `score`, `status`, `scorer_family`, `sub_scores{precision,recall,f1,reward,fbeta_beta}`,
`diagnostics{ir_metrics{precision,recall,f1},task_time_seconds,token_cost_usd}`.
`scorer_family_distribution` per config is `{oracle_overlap_fbeta: 5}` — every
trial routed through the declared family with no silent fallback to recall.

## Reproduction

The fresh trial outputs live at
`/home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe/runs/`. ur8d's
prior runs/reports are preserved at the same path with `.codeprobe-ur8d` suffix.

To replay:

```bash
codeprobe run /home/ds/test_repos/gascity/gascity-mcp-comparison \
  --repeats 1 --max-cost-usd 20 --force-plain
codeprobe experiment aggregate \
  /home/ds/test_repos/gascity/gascity-mcp-comparison/.codeprobe
```

Trials are checkpointed; clear `runs/<config>/checkpoint.db` to force a re-run.
