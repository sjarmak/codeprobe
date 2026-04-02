# Plan: statistical-tests

## Step 1: Extend dataclasses (backwards-compatible defaults)

### ConfigSummary — add:

- `ci_lower: float = 0.0` — Wilson CI lower bound for pass_rate
- `ci_upper: float = 0.0` — Wilson CI upper bound for pass_rate
- `billing_model: str = "unknown"` — billing model label
- `sample_size_warning: str | None = None` — warning when N < 10

### PairwiseComparison — add:

- `p_value: float | None = None` — from McNemar's or Wilcoxon
- `effect_size: float | None = None` — Cliff's delta or Cohen's d
- `effect_size_method: str = ""` — "cliffs_delta" or "cohens_d"
- `ci_lower: float = 0.0` — CI lower for score_diff
- `ci_upper: float = 0.0` — CI upper for score_diff

## Step 2: Implement statistical functions

### `wilson_ci(passed: int, total: int, z: float = 1.96) -> tuple[float, float]`

- Standard Wilson score interval formula, ~5 lines, no scipy

### `mcnemars_exact_test(a_scores: list[float], b_scores: list[float]) -> float | None`

- Build 2x2 contingency from paired pass/fail
- Discordant pairs: b=n01, c=n10
- Use math.comb for binomial CDF (exact test), ~15 lines
- Return p-value or None if no discordant pairs

### `cliffs_delta(a: list[float], b: list[float]) -> float`

- Count dominance pairs, ~5 lines

### `cohens_d(a: list[float], b: list[float]) -> float`

- Pooled std, mean diff / pooled_std, ~5 lines

### `wilcoxon_test(a: list[float], b: list[float]) -> float | None`

- scipy.stats.wilcoxon on paired differences
- Return p-value or None if all differences are zero

## Step 3: Wire into existing functions

### `summarize_config` and `summarize_completed_tasks`:

- Compute wilson_ci(passed, total) and set ci_lower/ci_upper
- Set sample_size_warning when total < 10
- Set billing_model from most common cost_model in tasks

### `compare_configs`:

- Needs access to raw task scores — but currently only gets ConfigSummary
- Option: compare_configs stays summary-only, add new `compare_configs_detailed` that takes ConfigResults
- Better option: add optional task lists to compare_configs signature
- Decision: Add optional `a_tasks` and `b_tasks` params. When provided, compute stats. When not, leave None defaults.

## Step 4: Pin scipy

- Add `scipy>=1.11,<2` to dependencies list in pyproject.toml

## Step 5: Tests

- TestWilsonCI: n=20, passed=15, verify bounds ~(0.531, 0.913)
- TestMcNemar: known 2x2 table, verify p-value
- TestCliffsDelta: [1,1,1,0] vs [0,0,1,0], verify value
- TestCohensD: known means/stds
- TestCompareConfigsStatistical: verify fields populated
- TestSampleSizeWarning: N < 10 triggers warning
