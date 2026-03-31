---
name: interpret
description: Analyze eval results and get actionable recommendations. Compares configurations statistically, ranks by score and cost-efficiency, and produces reports with clear conclusions. Triggers on interpret results, analyze experiment, what worked best, compare results, review experiment, show me the results, which config won.
user-invocable: true
---

# Interpret

Analyze eval experiment results and tell users what they mean. Goes beyond showing numbers -- provides rankings, statistical comparisons, cost-efficiency analysis, and actionable recommendations.

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
  - **HTML** -- "Interactive HTML report you can open in a browser"

Map to `FORMAT`:
- Text: `--format text`
- JSON: `--format json`
- HTML: `--format html`

---

## Phase 1: Run Interpretation

Execute the codeprobe CLI:

```bash
codeprobe interpret {RESULTS_PATH} --format {FORMAT}
```

This:
1. Loads results from the experiment or run directory
2. Computes configuration rankings by score
3. Runs pairwise statistical comparisons (effect size, confidence intervals)
4. Analyzes cost-efficiency tradeoffs
5. Generates actionable recommendations

---

## Phase 2: Present Results

### Single-Config Mode

When only one configuration has results:

```
Results Summary for {config_name}:

  Tasks run:      {N}
  Mean score:     {avg}
  Pass rate:      {pass}/{N} ({pct}%)
  Total cost:     ${total}
  Cost per task:  ${avg_cost}

  Per-Task Breakdown:

  | Task ID              | Score | Cost    | Time   |
  |----------------------|-------|---------|--------|
  | repo-leak-fix-001    | 1.00  | $0.23   | 3m12s  |
  | repo-auth-feat-001   | 0.50  | $0.45   | 5m30s  |
```

Suggest adding a second configuration for comparison.

### Multi-Config Mode

When multiple configurations have results, present:

**Configuration Ranking:**

```
| Rank | Config          | Score | Cost/Task | Score/$  |
|------|-----------------|-------|-----------|----------|
| 1    | opus-with-mcp   | 0.87  | $0.48     | 1.81     |
| 2    | with-mcp        | 0.82  | $0.31     | 2.65     |
| 3    | baseline        | 0.77  | $0.17     | 4.53     |

Best overall:   opus-with-mcp  (highest score)
Best value:     with-mcp       (best balance of score and cost)
Most efficient: baseline       (highest score per dollar)
```

**Statistical Significance:**

For each pair, report mean delta, win/loss/tie record, and effect size (Cohen's d):
- |d| < 0.2: negligible difference
- 0.2 <= |d| < 0.5: small effect
- 0.5 <= |d| < 0.8: medium effect
- |d| >= 0.8: large effect

**Recommendations:**

Clear, concrete recommendations based on the analysis:
- Best config for day-to-day work
- Best config for cost-sensitive environments
- Best config for highest accuracy regardless of cost

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

  5. Generate HTML report for sharing:
     codeprobe interpret {RESULTS_PATH} --format html
```

---

## Quick Reference

| User says | What happens |
|-----------|-------------|
| `/interpret` | Auto-detect results, full analysis |
| `/interpret /path/to/results` | Analyze specific results |
| "what worked best?" | Same as `/interpret`, focus on rankings |
| "compare results" | Same as `/interpret`, focus on pairwise comparison |
| "show me the results" | Overview + rankings |
| "which config should I use?" | Jump to recommendations |
| "generate HTML report" | `/interpret` with `--format html` |
