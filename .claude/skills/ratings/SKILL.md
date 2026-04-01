---
name: ratings
description: Record and analyze agent session quality ratings (1-5 scale). Track quality trends across models, MCPs, skills, and task types. Export to CSV for external analysis. Triggers on rate session, record rating, session rating, quality rating, ratings summary, export ratings.
user-invocable: true
---

# Ratings -- Session Quality Tracker

Record micro-ratings (1-5) after coding sessions, then summarize trends across models, MCPs, skills, and task types. Builds a longitudinal quality signal that complements automated eval scores.

Invokes `codeprobe ratings` under the hood -- all operations run through the CLI, not Python imports.

---

## Subcommands

| Command           | Purpose                         |
| ----------------- | ------------------------------- |
| `ratings record`  | Record a session quality rating |
| `ratings summary` | Print summary statistics        |
| `ratings export`  | Export ratings as CSV           |

---

## Phase 1: Determine Intent

**Question 1** -- Header: "What would you like to do?"

- Options:
  - **Record a rating** -- "Rate the current or most recent session"
  - **View summary** -- "See quality trends and statistics"
  - **Export data** -- "Export ratings to CSV for external analysis"

---

## Phase 2a: Record Rating

If recording:

**Question 2** -- Header: "Session quality"

- "How would you rate this session? (1-5)"
- Scale:
  - **1** -- Poor: agent was unhelpful or produced incorrect results
  - **2** -- Below average: required significant manual correction
  - **3** -- Acceptable: got the job done with some guidance
  - **4** -- Good: efficient and mostly autonomous
  - **5** -- Excellent: exceeded expectations, minimal intervention

**Question 3** -- Header: "Session context" (optional)

- "What type of task was this?" -- Options: **bugfix**, **feature**, **refactor**, **debug**, **docs**, **other**
- "Approximate session duration?" (in minutes, optional)
- "Number of tool calls?" (optional, may be auto-detected)

Run the record command:

```bash
codeprobe ratings record {RATING} \
  --path ratings.jsonl \
  {--task-type TASK_TYPE} \
  {--duration DURATION_SEC} \
  {--tool-calls TOOL_CALLS}
```

Confirm: "Recorded rating={RATING} for {model}. Total ratings: {count}"

---

## Phase 2b: View Summary

If viewing summary:

```bash
codeprobe ratings summary --path ratings.jsonl
```

Display the output, which includes:

- Overall mean rating and count
- Breakdown by model, MCPs, skills, and task type
- Sample size warnings (need >= 15 for reliable conclusions)

### Interpreting Results

- **Mean < 3.0**: Investigate -- which dimension is dragging quality down?
- **High stdev**: Inconsistent quality -- look for patterns in low-rated sessions
- **Model comparison**: If one model consistently rates higher, consider switching defaults
- **MCP impact**: MCPs that correlate with higher ratings are worth keeping enabled

---

## Phase 2c: Export Data

If exporting:

**Question** -- Header: "Export path"

- "Where should I save the CSV?"
- Default: `ratings_export.csv`

```bash
codeprobe ratings export --path ratings.jsonl --output "{OUTPUT_PATH}"
```

### CSV Columns

| Column       | Description                          |
| ------------ | ------------------------------------ |
| `ts`         | ISO timestamp                        |
| `rating`     | 1-5 score                            |
| `model`      | Model used (e.g., claude-sonnet-4-6) |
| `mcps`       | Comma-separated MCP servers active   |
| `skills`     | Comma-separated skills used          |
| `task_type`  | bugfix, feature, refactor, etc.      |
| `duration_s` | Session duration in seconds          |
| `tool_calls` | Number of tool calls                 |

---

## Rating Storage

Ratings are stored in JSONL format (one JSON object per line):

```jsonl
{
  "ts": "2026-04-01T12:30:00Z",
  "rating": 4,
  "config": {
    "model": "claude-sonnet-4-6",
    "mcps": [
      "context7"
    ],
    "skills": [
      "tdd"
    ]
  },
  "task_type": "bugfix",
  "duration_s": 180,
  "tool_calls": 23
}
```

Default path: `ratings.jsonl` in the current directory. Override with `--path`.

---

## Integration with Other Skills

- `/run-eval`: Automated scoring -- ratings add the human quality signal
- `/interpret`: Combine eval scores with session ratings for a complete picture
- `/experiment`: Track how config changes (model, MCPs, skills) affect perceived quality

---

## Quick Reference

| User says                       | What happens                                  |
| ------------------------------- | --------------------------------------------- |
| `/ratings`                      | Full guided flow (record, summary, or export) |
| "rate this session 4"           | Record rating=4 directly                      |
| "show ratings"                  | Display summary statistics                    |
| "export ratings to csv"         | Export to ratings_export.csv                  |
| "how are my sessions trending?" | Show summary with interpretation              |
