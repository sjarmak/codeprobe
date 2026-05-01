# codeprobe-ttwq — oracle_checks N=3 rerun

Reruns the 5 `oracle_checks` tasks from codeprobe-3oms at **N=3** to test whether the −0.071 family-level penalty (and oc_004's −0.357 single-trial drop) reproduce.

× 2 configs (baseline, with-sourcegraph) × N=3 = **30 trials**. Model: claude-sonnet-4-6. Cost: $9.85.

## Headline

| family        | n_pairs | baseline_mean | with_sg_mean | delta   | paired-t (df=14) | 95% CI            |
|---------------|---------|---------------|--------------|---------|------------------|-------------------|
| oracle_checks | 15      | 1.000         | 0.914        | −0.0857 | t=−1.81 (p≈0.092) | [−0.187, +0.016]  |

Family-level delta **not significant at α=0.05**, but driven entirely by oc_004:

| task   | baseline | with-sg                  | delta  | t    | 95% CI            |
|--------|----------|--------------------------|--------|------|-------------------|
| oc_001 | [1,1,1]  | [1,1,1]                  | 0.000  | n/a  | [0, 0]            |
| oc_002 | [1,1,1]  | [1,1,1]                  | 0.000  | n/a  | [0, 0]            |
| oc_003 | [1,1,1]  | [1,1,1]                  | 0.000  | n/a  | [0, 0]            |
| oc_004 | [1,1,1]  | [0.643, 0.429, 0.643]    | **−0.429** | **−6.0** | [−0.736, −0.121] |
| oc_005 | [1,1,1]  | [1,1,1]                  | 0.000  | n/a  | [0, 0]            |

**oc_004 is reproducibly broken under Sourcegraph.** All three with-sg trials confidently denied that `FlagAliases` exists in the codebase. It does — `internal/config/provider.go:34`. The mechanism is a Sourcegraph false-negative cascade, not the "thoroughness penalty" hypothesized by 3oms.

See [eval_writeup.md](./eval_writeup.md) for the full diagnosis (including verbatim agent quotes and rubric breakdown).

## Files

- [`eval_writeup.md`](./eval_writeup.md) — narrative writeup with per-task table, paired-t test, oc_004 case study, and Sourcegraph false-negative analysis.
- [`aggregate.json`](./aggregate.json) — `codeprobe interpret --format json` envelope.
- [`per_trial.json`](./per_trial.json) — flat list of 30 trials (config, task_id, repeat_index, reward, cost, tokens, scorer_family).
- [`per_family_summary.json`](./per_family_summary.json) — per-config / per-task aggregates and paired-t deltas.
- [`analyze.py`](./analyze.py) — per-task aggregation + paired-t computation.

## Reproduction

```bash
# Sister experiment dir to avoid tenant-lock collision with codeprobe-mcn7
codeprobe run /home/ds/test_repos/gascity/gascity-oc-rerun-ttwq \
  --tenant codeprobe-ttwq \
  --repeats 3 --max-cost-usd 15 --force-plain
codeprobe interpret /home/ds/test_repos/gascity/gascity-oc-rerun-ttwq --format json \
  > docs/investigations/codeprobe-ttwq/aggregate.json
python3 docs/investigations/codeprobe-ttwq/analyze.py
```

Run artifacts preserved at `/home/ds/test_repos/gascity/gascity-oc-rerun-ttwq/.codeprobe/runs/{baseline,with-sourcegraph}/`. Per-task directories carry the last trial; per-repeat subdirectories (`repeat-1/`, `repeat-2/`) preserve the earlier trials' agent_output and scoring.
