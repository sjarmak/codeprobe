# Research: Worktree Isolation

## executor.py

- `execute_config()` dispatches tasks via ThreadPoolExecutor when parallel > 1
- `_git_reset_workdir()` runs `git checkout -- .` && `git clean -fd` between sequential tasks
- Sequential mode resets repo between tasks; parallel mode does NOT reset
- Cost circuit-breaker halts when cumulative cost exceeds budget

## preamble.py

- `_base_prompt(instruction, repo_path)` embeds repo_path in prompt string
- `compose_instruction()` calls `_base_prompt()` and appends preamble blocks
- repo_path is passed through as `Path` — easy to override with worktree path

## protocol.py

- `AgentAdapter` Protocol: `name` (property), `preflight(config)`, `run(prompt, config)`
- `AgentConfig`: model, permission_mode, timeout_seconds, mcp_config, extra, cwd
- `AgentOutput`: frozen dataclass with stdout/stderr/exit_code/duration/tokens/cost

## claude.py

- `ClaudeAdapter` extends `BaseAdapter`, uses `JsonStdoutCollector`
- `build_command()` builds `claude -p <prompt> --output-format json`
- No session isolation currently — all runs share parent env

## \_base.py

- `BaseAdapter.run()` calls subprocess.run with `cwd=config.cwd` and `env=_adapter_safe_env()`
- `_adapter_safe_env()` whitelists specific env vars, accepts `extra` dict overlay
- `_ADAPTER_ENV_WHITELIST` includes CLAUDE_CONFIG_DIR? No — not currently whitelisted

## Key integration points

1. executor.py parallel branch needs worktree acquire/release around each task
2. preamble needs optional worktree_path override for repo_path
3. \_base.py `_adapter_safe_env()` needs to accept CLAUDE_CONFIG_DIR from isolate_session()
4. `_ADAPTER_ENV_WHITELIST` needs CLAUDE_CONFIG_DIR added
