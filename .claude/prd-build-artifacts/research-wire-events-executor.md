# Research: wire-events-executor

## events.py API

- Event types: RunStarted, TaskStarted, TaskScored, BudgetWarning, RunFinished (all frozen dataclasses)
- RunEvent = Union of all five
- RunEventListener protocol: `on_event(self, event: RunEvent) -> None`
- EventDispatcher: queue-based with daemon thread, `register()`, `emit()`, `shutdown()`
- BudgetChecker: listener that tracks cumulative cost, emits BudgetWarning at threshold crossings
  - `set_dispatcher()` for back-reference
  - `is_exceeded` property (threading.Event based)
  - Only counts `per_token` cost_model
  - Has 80% warning threshold and 100% exceeded threshold

## executor.py

- `execute_config()` signature: adapter, task_dirs, repo_path, experiment_config, agent_config, plus kwargs including on_task_complete callback, max_cost_usd, parallel
- `_handle_result()` inner function: appends result, saves artifacts, calls on_task_complete, accumulates cost, emits 80% warning via `_budget_msg()`
- Sequential path: checks `cumulative_cost > max_cost_usd` before each task
- Parallel path: checks after each completed future
- `_budget_msg()` writes to sys.stderr directly
- `_BILLABLE_COST_MODELS = frozenset({"per_token"})` and `_BUDGET_WARNING_THRESHOLD = 0.80`

## run_cmd.py

- `_on_task_complete(result)`: prints PASS/FAIL + duration to stdout via click.echo
- `run_eval()` calls `execute_config()` with `on_task_complete=_on_task_complete`
- Thread-safe sandbox via `_acquire_sandbox`/`_release_sandbox`
- No event dispatcher wiring yet

## Key integration points

- execute_config needs optional event_dispatcher param
- \_handle_result needs to emit TaskScored
- Budget checking can delegate to BudgetChecker when dispatcher present
- run_cmd.py needs PlainTextListener + dispatcher lifecycle
