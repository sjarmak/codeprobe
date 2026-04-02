# Plan: Repeat Infrastructure

## 1. Add repeat_index to CompletedTask (experiment.py)

- Add `repeat_index: int = 0` field to CompletedTask dataclass
- Default 0 maintains backward compatibility

## 2. Modify CheckpointStore (checkpoint.py)

- Add `repeat_index` column to schema (migration for existing DBs)
- Change PRIMARY KEY to (task_id, config_name, repeat_index)
- Update `append()` to include repeat_index in INSERT/UPSERT
- Update `load_ids()` to return set of (task_id, repeat_index) tuples
- Update `load_entries()` to include repeat_index in returned dicts

## 3. Modify Executor (executor.py)

- Add `repeats: int = 1` parameter to `execute_config()`
- Build expanded task list: each task_dir × range(repeats)
- `_restore_checkpointed()` returns set of (task_id, repeat_index) tuples
- Skip logic uses (task_id, repeat_index) instead of just task_id
- `_handle_result()` passes CompletedTask with correct repeat_index
- Artifact saving uses `{task_id}/repeat-{repeat_index}/` subdirectory

## 4. Modify run_cmd.py

- Add `repeats: int = 1` parameter to `run_eval()`
- Pass `repeats` to `execute_config()`

## 5. interpret_cmd.py aggregation

- No changes needed — repeats appear as separate CompletedTask entries
- summarize_config already computes mean scores across all entries
- The repeat_index field is informational for grouping if needed later

## 6. Backward compatibility

- Default repeat_index=0 means existing data works unchanged
- Schema migration adds column with default 0
- New PRIMARY KEY handles old data (all have repeat_index=0)
