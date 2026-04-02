# Research: sandbox-detection

## ALLOWED_PERMISSION_MODES

- Location: `src/codeprobe/adapters/protocol.py:8`
- Current value: `frozenset({"default", "plan", "auto", "acceptEdits"})`
- Need to add: `"dangerously_skip"`

## ClaudeAdapter.build_command()

- Location: `src/codeprobe/adapters/claude.py:28-47`
- When `permission_mode != "default"`, validates against ALLOWED_PERMISSION_MODES, then appends `--permission-mode <mode>`
- For `dangerously_skip`, we need a different flag: `--dangerously-skip-permissions` (no `--permission-mode` prefix)

## BaseAdapter.preflight()

- Location: `src/codeprobe/adapters/_base.py:89-93`
- Default: checks if binary exists
- ClaudeAdapter does NOT override preflight — it inherits from BaseAdapter
- Need to override preflight in ClaudeAdapter to add sandbox check

## Test file

- `tests/test_adapters.py` — extensive tests for protocol, base adapter, claude/copilot parse_output
- Existing test `test_claude_rejects_bypass_permissions` tests ValueError for unsafe modes
- Tests use `unittest.mock.patch` for subprocess mocking

## Core directory

- `src/codeprobe/core/` exists with: checkpoint.py, executor.py, experiment.py, isolation.py, llm.py, preamble.py, registry.py, scoring.py
- `sandbox.py` does not exist yet — needs creation
