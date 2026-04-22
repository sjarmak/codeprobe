---
name: interpret
description: Analyze eval results and get actionable recommendations. Compares configurations statistically, ranks by score and cost-efficiency, and produces reports with clear conclusions. Triggers on interpret results, analyze experiment, what worked best, compare results, review experiment, show me the results, which config won.
user-invocable: true
---

# Interpret

Analyze eval experiment results and tell users what they mean. Goes beyond showing numbers -- provides rankings, pairwise comparisons, cost-efficiency analysis, and actionable recommendations.

Invokes `codeprobe interpret` under the hood -- all analysis runs through the CLI, not Python imports.

Works with:

- Experiment directories (from `codeprobe run` with `--config`) -- full multi-config analysis
- Single run directories -- single-config summary

---

## Phase 0: Interpretation Configuration

Ask the user:

**Question 1** -- Header: "Results source"

- Question: "Where are the eval results?"
- Options:
  - **Auto-detect** -- "Look for results in the current directory"
  - **Specific path** -- "I'll provide a path to a results or experiment directory"

If **Auto-detect**, look for results in this order:

1. `experiments/*/experiment.json` in the current directory
2. `*-eval/results.json` or `*/results.json` in the current directory
3. Ask the user for a path

If **Specific path**, prompt for the path and set `RESULTS_PATH={user_input}`.

### Validate Results

```bash
[ -f {RESULTS_PATH}/results.json ] || [ -f {RESULTS_PATH}/experiment.json ] && echo "valid" || echo "no results found"
```

If no results found, suggest running `codeprobe run` first.

**Question 2** -- Header: "Output format"

- Question: "How should I format the analysis?"
- Options:
  - **Text** -- "Plain text summary in the terminal"
  - **JSON** -- "Machine-readable JSON output"

Map to `FORMAT`:

- Text: `--format text`
- JSON: `--format json`

---

## Phase 1: Run Interpretation

Execute the codeprobe CLI:

```bash
codeprobe interpret {RESULTS_PATH} --format {FORMAT}
```

This:

1. Loads results from the experiment or run directory
2. Computes configuration rankings by score and cost-efficiency
3. Runs pairwise comparisons (score diff, cost diff, speed diff, winner)
4. Detects incomplete sweeps and flags partial results
5. Generates actionable recommendations

---

## Phase 2: Present Results

### Single-Config Mode

When only one configuration has results:

```
## Experiment: {name}

### Rankings
1. {config} — {pass_rate}% pass rate, ${total_cost} total — {recommendation}

### Recommendation
Use {config} for best results.
```

Suggest adding a second configuration for comparison.

### Multi-Config Mode

When multiple configurations have results, present:

**Rankings** (sorted by score, with cost and recommendation):

```
### Rankings
1. opus-with-mcp — 87% pass rate, $4.80 total — Best overall
2. with-mcp — 82% pass rate, $3.10 total — Best cost-efficiency
3. baseline — 77% pass rate, $1.70 total — Most efficient
```

**Detailed Comparison** (pairwise summaries showing score diff, cost diff, speed diff, and winner):

```
### Detailed Comparison
opus-with-mcp vs with-mcp: +0.05 score, +$0.17/task cost, ...
```

**Recommendations** — clear, concrete advice:

- Best config for day-to-day work
- Best config for cost-sensitive environments

### Partial Results

When a sweep is incomplete, the report flags it:

```
**PARTIAL** 3/5 tasks (60%)
```

---

## Phase 3: Next Steps

```
Analysis complete. Next steps:

  1. {If sample size small}: Run more tasks for higher confidence.
     codeprobe mine {REPO_PATH} --count 15

  2. {If one config clearly wins}: Adopt {config} as your default setup.

  3. {If configs are close}: Run more tasks to get statistical separation.
     Focus on harder tasks where configs diverge most.

  4. Compare different models:
     codeprobe run {REPO_PATH} --agent claude --model claude-opus-4-6
```

---

## Planned (Coming Soon)

These features are not yet implemented:

- **Cohen's d effect size** — statistical effect size for pairwise comparisons
- **Confidence intervals** — CI bounds on score and cost metrics
- **Per-task breakdown tables** — detailed task-level score/cost/time tables
- **HTML reports** — interactive browser-based reports (`--format html`)
- **CSV export** — machine-readable tabular output (`--format csv`)

---

## Quick Reference

| User says                     | What happens                                       |
| ----------------------------- | -------------------------------------------------- |
| `/interpret`                  | Auto-detect results, full analysis                 |
| `/interpret /path/to/results` | Analyze specific results                           |
| "what worked best?"           | Same as `/interpret`, focus on rankings            |
| "compare results"             | Same as `/interpret`, focus on pairwise comparison |
| "show me the results"         | Overview + rankings                                |
| "which config should I use?"  | Jump to recommendations                            |
