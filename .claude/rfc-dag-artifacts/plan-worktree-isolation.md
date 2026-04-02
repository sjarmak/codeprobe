# Plan: Worktree Isolation

## IsolationStrategy Protocol

```python
class IsolationStrategy(Protocol):
    def acquire(self) -> Path: ...       # Get an isolated workspace
    def reset(self, workspace: Path) -> None: ...  # Reset to clean state
    def release(self, workspace: Path) -> None: ... # Return to pool
    def cleanup(self) -> None: ...       # Remove all worktrees
```

## WorktreeIsolation

- Pool of N worktrees created in `<repo>/.codeprobe-worktrees/slot-{i}`
- `acquire()`: pop from available queue (blocking), return worktree path
- `release()`: reset + push back to available queue
- `reset()`: git checkout -- . && git clean -fd in the worktree
- `cleanup()`: remove all worktrees via `git worktree remove`

## Executor Integration

- When parallel > 1, create WorktreeIsolation(repo_path, pool_size=parallel)
- Each task: acquire worktree → set as repo_path in execute_task → release
- Pass worktree_path through to preamble for prompt rewriting
- Global `threading.Semaphore(max_concurrent)` caps total subprocess count

## Preamble Changes

- `_base_prompt()` accepts optional `worktree_path` parameter
- When set, prompt uses worktree_path instead of repo_path

## Protocol Changes

- Add `isolate_session(slot_id: int) -> dict[str, str]` to AgentAdapter
- Returns env dict for session isolation (e.g. CLAUDE_CONFIG_DIR)
- Default implementation returns empty dict

## Claude Adapter

- `isolate_session(slot_id)` returns `{"CLAUDE_CONFIG_DIR": "<tmpdir>/slot-{slot_id}"}`
- Add CLAUDE_CONFIG_DIR to \_ADAPTER_ENV_WHITELIST
