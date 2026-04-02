# Research: statistical-tests

## Current State

### Dataclasses

- `ConfigSummary` (frozen): label, total_tasks, completed, errored, pass_rate, mean_score, median_score, total_duration_sec, mean_duration_sec, total_cost_usd, total_tokens, is_partial, tasks_expected
- `PairwiseComparison` (frozen): config_a, config_b, score_diff, cost_diff, speed_diff, winner, summary

### Functions

- `summarize_config(results, *, total_tasks)` → ConfigSummary
- `summarize_completed_tasks(label, tasks, *, total_tasks)` → ConfigSummary (streaming variant)
- `compare_configs(a, b)` → PairwiseComparison
- `_determine_winner(a, b)` → str

### Key Details

- PASS_THRESHOLD = 0.5 — binary pass/fail derived from automated_score
- CompletedTask has: task_id, automated_score, status, duration_seconds, input_tokens, output_tokens, cost_usd, cost_model, cost_source
- Tests use `_task()` helper for creating CompletedTask instances
- All dataclasses use `frozen=True`

### Test Patterns

- Test classes organized by function (TestSummarizeConfig, TestCompareConfigs, etc.)
- `pytest.approx()` for float comparison
- ConfigSummary constructed directly in compare_configs tests (no summarize_config call)

## Statistical Methods Needed

1. **McNemar's exact test** — for paired binary (pass/fail) data, uses binomial test on discordant pairs
2. **Wilcoxon signed-rank** — for paired continuous scores, needs scipy.stats.wilcoxon
3. **Cliff's delta** — nonparametric effect size for binary/ordinal data
4. **Cohen's d** — parametric effect size for continuous data
5. **Wilson score interval** — CI for binomial proportions (pass rates)
