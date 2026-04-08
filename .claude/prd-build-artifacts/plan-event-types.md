# Plan: event-types

## Step 1: Create src/codeprobe/core/events.py

1. Imports: dataclasses, queue, threading, time, typing (Protocol, Union, runtime_checkable)
2. Define 5 frozen dataclass event types per spec
3. Define RunEvent = Union of all 5
4. Define RunEventListener Protocol with on_event(event: RunEvent) -> None
5. Define EventDispatcher class with queue, listeners list, daemon thread
6. Define BudgetChecker class implementing RunEventListener

## Step 2: Create tests/test_events.py

1. Test event creation (all 5 types, verify frozen)
2. Test EventDispatcher delivers to multiple listeners in order
3. Test emit() is non-blocking (measure time < 10ms)
4. Test BudgetChecker fires exceeded at correct threshold
5. Test BudgetChecker emits BudgetWarning at 80% and 100%
6. Test shutdown() drains all queued events before returning
7. Test thread safety: 10 concurrent emit() calls all delivered

## Step 3: Run tests

pytest tests/test_events.py -v

## Step 4: Commit
