# Plan: CSV Per-Task Report

## Changes

### 1. Report dataclass — add config_results field

- Add `config_results: tuple[ConfigResults, ...] = ()` to Report
- Populate in generate_report(); leave empty in generate_report_streaming()

### 2. format_csv_report(report: Report) -> str

- Use stdlib csv.writer with io.StringIO
- Header comment when any summary has sample_size_warning: `# SINGLE RUN — no statistical confidence`
- Columns: config, task_id, repeat, score, pass, duration_sec, cost_usd, cost_source, input_tokens, output_tokens, cache_read_tokens, cost_model, ci_lower, ci_upper
- repeat=1 always (single run per task in current model)
- pass = 1 if score > 0 else 0
- ci_lower/ci_upper from parent ConfigSummary (same for all tasks in config)

### 3. format_text_report — add per-task table

- After Rankings section, add "### Per-Task Results" with tabular output
- Columns: Config | Task | Score | Pass | Duration | Cost

### 4. format_json_report — add per-task data

- Add "tasks" array with per-task dicts matching CSV columns

### 5. interpret_cmd.py — add csv format

- Dispatch fmt=="csv" to format_csv_report

### 6. CLI **init**.py — update help text

- Change help to "text, json, csv"

### 7. analysis/**init**.py — export format_csv_report

### 8. Tests

- TestFormatCsvReport: verify header, columns, warning comment, data rows
- TestFormatTextReport: verify per-task table present
- TestFormatJsonReport: verify tasks array present
