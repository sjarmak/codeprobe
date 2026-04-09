# Research: event-types

## Data available at each lifecycle point

### RunStarted

- `len(task_dirs)` — total tasks (executor.py:466 task_dirs param)
- `experiment_config.label` — config label (executor.py:454)
- timestamp: `time.time()`

### TaskStarted

- `task_dir.name` — task_id (executor.py:517)
- `experiment_config.label` — config label
- timestamp

### TaskScored (from CompletedTask, models/experiment.py:24-40)

- `task_id: str`
- `automated_score: float`
- `duration_seconds: float`
- `cost_usd: float | None`
- `input_tokens: int | None`
- `output_tokens: int | None`
- `cache_read_tokens: int | None`
- `cost_model: str` (e.g. "per_token", "unknown", "subscription")
- `cost_source: str`
- `error_category: str | None` (maps to error field in event)

### BudgetWarning

- `cumulative_cost` — accumulated in executor.py:508,572
- `max_cost_usd` — budget param (executor.py:460)
- `_BUDGET_WARNING_THRESHOLD` — 0.8 (executor.py:578)

### RunFinished

- `len(task_dirs)` — total_tasks
- `len(results)` — completed_count
- mean_score: computed from results
- total_cost: sum of cost_usd where billable
- total_duration: sum of duration_seconds

## Billable cost models

- `_BILLABLE_COST_MODELS` in executor.py includes "per_token" (line 571)
- Non-billable: "unknown", "subscription"

## Threading model

- executor already uses ThreadPoolExecutor for parallel tasks (line 640)
- Budget checking is sequential within `_handle_result` (nonlocal cumulative_cost)
- Event dispatcher daemon thread is safe: queue.Queue is thread-safe
