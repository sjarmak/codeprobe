# Research: CSV Per-Task Report

## Current State

### Report dataclass (report.py:20-30)

- Fields: experiment_name, summaries, rankings, comparisons, is_partial, tasks_expected, completion_ratio
- Does NOT store raw CompletedTask data — only aggregated ConfigSummary

### CompletedTask fields (models/experiment.py:24-38)

- task_id, automated_score, status, duration_seconds
- input_tokens, output_tokens, cache_read_tokens (all nullable)
- cost_usd (nullable), cost_model, cost_source
- scoring_details, metadata

### format_text_report (report.py:131-180)

- Shows rankings + comparisons + recommendation
- No per-task breakdown

### format_json_report (report.py:183-202)

- Serializes summaries/rankings/comparisons via asdict()
- No per-task data

### interpret_cmd.py (interpret_cmd.py:27-66)

- `run_interpret(path, fmt="text")` dispatches on fmt == "json" vs default text
- CLI option: `--format` with help "text, json, html"

### CLI registration (cli/**init**.py:128-141)

- @main.command interpret with --format option
- Passes fmt to run_interpret

### ConfigSummary (stats.py:139-158)

- Has ci_lower, ci_upper fields
- Has sample_size_warning (set when repeats=1)

## Key Finding

Report doesn't carry raw tasks. Need to add config_results to Report for per-task output.
