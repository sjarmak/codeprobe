# Research: rich-dashboard

## Event System (src/codeprobe/core/events.py)

- RunEventListener protocol: single method `on_event(event: RunEvent) -> None`
- RunEvent = Union[RunStarted, TaskStarted, TaskScored, BudgetWarning, RunFinished]
- Events are frozen dataclasses (immutable)
- EventDispatcher uses a daemon thread with queue-based fan-out
- Events delivered on dispatcher thread, not caller thread

### Event Fields

- RunStarted: total_tasks, config_label, timestamp
- TaskStarted: task_id, config_label, timestamp
- TaskScored: task_id, config_label, automated_score, duration_seconds, cost_usd (optional), input/output/cache tokens (optional), cost_model, cost_source, error (optional), timestamp
- BudgetWarning: cumulative_cost, budget, threshold_pct, timestamp
- RunFinished: total_tasks, completed_count, mean_score, total_cost, total_duration, timestamp

## run_cmd.py Structure

- PlainTextListener at line 35-59: handles TaskScored, BudgetWarning, RunFinished
- \_run_config() at line 221: creates EventDispatcher, registers PlainTextListener (line 280)
- run_eval() accepts: path, agent, model, config, max_cost_usd, parallel, repeats, dry_run
- No --quiet flag currently passed through; quiet is on the main group
- CLI run command defined in cli/**init**.py lines 284-335 with click decorators

## pyproject.toml

- Dependencies: click, pyyaml, anthropic, openai, tiktoken, scipy
- No rich dependency yet
- Python >=3.11

## Key Observations

- Events dispatched on daemon thread - Rich Live.update() is thread-safe, good match
- PlainTextListener import in run_cmd.py is local (class defined in same file)
- The run command in **init**.py does lazy import of run_eval
- Need to pass --force-plain/--force-rich through run_eval to \_run_config
- \_run_config is a nested function inside run_eval - flags need to be in run_eval scope
