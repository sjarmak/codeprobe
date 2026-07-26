# nested_tasks fixture

Acceptance fixture for `BUG-VALIDATE-DISCOVERY-005`: `codeprobe validate`
must discover task directories nested below the argument, at more than one
depth. Both tasks are fully valid so `validate` exits 0 — a task that failed
validation would let the `cli_stdout_contains` criterion green vacuously.

- `group-a/task-001`            — one grouping level deep
- `group-b/subgroup/task-002`   — two grouping levels deep
