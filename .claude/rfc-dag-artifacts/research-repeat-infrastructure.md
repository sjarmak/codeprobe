# Research: Repeat Infrastructure

## CompletedTask (experiment.py)

- Frozen dataclass with fields: task_id, automated_score, status, duration_seconds, token fields, cost fields, scoring_details, metadata
- No repeat_index field currently
- Primary key in checkpoint DB is (task_id, config_name)

## Executor (executor.py)

- `execute_config()` dispatches tasks via `_run_one()`, handles sequential/parallel modes
- `_restore_checkpointed()` loads checkpoint entries, builds set of IDs to skip
- `_handle_result()` appends to results, saves artifacts, appends to checkpoint
- Checkpoint skip uses `d.name not in checkpointed_ids` (set of task_id strings)

## CheckpointStore (checkpoint.py)

- SQLite-backed, WAL mode
- Table schema: PRIMARY KEY (task_id, config_name)
- `append()` uses INSERT OR UPDATE on (task_id, config_name) conflict
- `load_ids()` returns set of task_id strings (status != 'error')
- `load_entries()` returns list of dicts from result_json column

## run_cmd.py

- `run_eval()` function called by CLI
- Creates AgentConfig, resolves adapter, calls `execute_config()`
- No --repeats parameter currently

## interpret_cmd.py

- Loads ConfigResults via `load_config_results()`, passes to `generate_report()`
- `generate_report()` calls `summarize_config()` which operates on list[CompletedTask]
- No repeat-aware aggregation needed at analysis level — repeats appear as separate CompletedTask entries, and summarize_config already computes mean scores across all entries
