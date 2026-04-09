# Plan: wire-events-executor

## executor.py changes

1. Add `event_dispatcher` optional param to `execute_config()`:

   ```python
   event_dispatcher: EventDispatcher | None = None
   ```

2. Import events module at top:

   ```python
   from codeprobe.core.events import (
       EventDispatcher, BudgetChecker, RunStarted, TaskStarted, TaskScored, RunFinished,
   )
   ```

3. At start of execute_config, after restoring checkpoints:
   - If event_dispatcher provided and max_cost_usd set: create BudgetChecker, register with dispatcher, set_dispatcher back-reference
   - Emit RunStarted event

4. Before each `_run_one` call (both sequential and parallel paths):
   - Emit TaskStarted event

5. In `_handle_result`:
   - If event_dispatcher provided: emit TaskScored event with fields from CompletedTask
   - Keep on_task_complete callback for backward compat (always call if provided)
   - Budget warning: when dispatcher present, BudgetChecker handles it. When not present, keep inline logic.

6. Budget halt check:
   - When dispatcher + BudgetChecker: check `budget_checker.is_exceeded` instead of `cumulative_cost > max_cost_usd`
   - When no dispatcher: keep existing inline check
   - Keep \_budget_msg() for the "halting" message (or emit via dispatcher)

7. After task loop: emit RunFinished with summary stats

## run_cmd.py changes

1. Create PlainTextListener class (in run_cmd.py, small enough):
   - on_event handles TaskScored (PASS/FAIL line), BudgetWarning (stderr), RunFinished (optional summary)

2. In `_run_config`:
   - Create EventDispatcher
   - Create PlainTextListener, register
   - Pass dispatcher to execute_config
   - In finally: dispatcher.shutdown()
   - Remove on_task_complete=\_on_task_complete (PlainTextListener replaces it)

## Backward compat

- on_task_complete still works when no dispatcher provided
- \_budget_msg() still used for halt message in non-dispatcher path
- All existing tests pass unchanged

## Test plan (test_executor_events.py)

- TestEventOrder: 3-task run emits RunStarted, 3x(TaskStarted, TaskScored), RunFinished in order
- TestBudgetCheckerIntegration: max_cost triggers BudgetWarning and halts
- TestBackwardCompat: on_task_complete without dispatcher still called
- TestPlainTextListener: captures expected stderr/stdout output
