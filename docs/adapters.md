# Adapter Authoring Guide

This guide explains how to add support for a new AI coding agent to codeprobe.

## Architecture Overview

codeprobe uses a Protocol-based adapter system. Every agent integration
implements the same three-method interface, which lets the eval runner treat
all agents identically regardless of whether they run as a CLI subprocess or
hit an HTTP API.

There are two common patterns:

| Pattern         | Base class                         | When to use                                                              |
| --------------- | ---------------------------------- | ------------------------------------------------------------------------ |
| **CLI adapter** | `BaseAdapter`                      | Agent is invoked via a subprocess (e.g. `claude -p`, `aider --message`)  |
| **API adapter** | None (implement Protocol directly) | Agent is called via a Python SDK (e.g. OpenAI `client.responses.create`) |

## The AgentAdapter Protocol

Defined in `src/codeprobe/adapters/protocol.py`:

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class AgentAdapter(Protocol):
    @property
    def name(self) -> str:
        """Human-readable agent name (e.g. 'claude', 'aider')."""
        ...

    def preflight(self, config: AgentConfig) -> list[str]:
        """Validate readiness. Return a list of issues (empty = ready)."""
        ...

    def run(self, prompt: str, config: AgentConfig) -> AgentOutput:
        """Execute the agent and return results."""
        ...
```

Because `AgentAdapter` is a `@runtime_checkable` Protocol, you never need to
inherit from it. Any class with `name`, `preflight`, and `run` satisfies the
contract:

```python
class MinimalAdapter:
    @property
    def name(self) -> str:
        return "my-agent"

    def preflight(self, config: AgentConfig) -> list[str]:
        return []

    def run(self, prompt: str, config: AgentConfig) -> AgentOutput:
        return AgentOutput(stdout="ok", stderr=None, exit_code=0, duration_seconds=0.1)

assert isinstance(MinimalAdapter(), AgentAdapter)  # passes
```

## Data Types

### AgentConfig

Configuration passed to every adapter method:

| Field             | Type           | Default     | Notes                                                      |
| ----------------- | -------------- | ----------- | ---------------------------------------------------------- |
| `model`           | `str \| None`  | `None`      | Model override (adapter picks its own default when `None`) |
| `permission_mode` | `str`          | `"default"` | Values: `default`, `plan`, `auto`, `acceptEdits`           |
| `timeout_seconds` | `int`          | `300`       | Maximum execution time                                     |
| `mcp_config`      | `dict \| None` | `None`      | MCP tool configuration                                     |
| `extra`           | `dict \| None` | `None`      | Adapter-specific options                                   |
| `cwd`             | `str \| None`  | `None`      | Working directory for the agent                            |

### AgentOutput

Immutable dataclass returned by `run()`:

| Field               | Type            | Default         | Notes                                               |
| ------------------- | --------------- | --------------- | --------------------------------------------------- |
| `stdout`            | `str`           | required        | Agent's primary output                              |
| `stderr`            | `str \| None`   | required        | Standard error (or `None`)                          |
| `exit_code`         | `int`           | required        | `0` = success, `-1` = timeout                       |
| `duration_seconds`  | `float`         | required        | Wall-clock time                                     |
| `cost_usd`          | `float \| None` | `None`          | Estimated cost in USD                               |
| `input_tokens`      | `int \| None`   | `None`          | Input/prompt tokens                                 |
| `output_tokens`     | `int \| None`   | `None`          | Output/completion tokens                            |
| `cache_read_tokens` | `int \| None`   | `None`          | Prompt-cache hits                                   |
| `cost_model`        | `str`           | `"unknown"`     | See [cost_model values](#cost_model-values)         |
| `cost_source`       | `str`           | `"unavailable"` | See [cost_source values](#cost_source-values)       |
| `tool_call_count`   | `int \| None`   | `None`          | Number of `tool_use` blocks in agent output         |
| `error`             | `str \| None`   | `None`          | Error description (partial results still preserved) |

Validation rules enforced by `__post_init__`:

- `cost_model` must be one of the allowed values (see below).
- `cost_source` must be one of the allowed values (see below).
- When `cost_model` is `"per_token"`, `cost_usd` is required (raises `ValueError` otherwise).

### cost_model values

| Value          | Meaning                                                                 |
| -------------- | ----------------------------------------------------------------------- |
| `per_token`    | Agent charges per token. `cost_usd` must be set.                        |
| `subscription` | Agent is a flat-rate subscription (e.g. Copilot). `cost_usd` is `None`. |
| `unknown`      | Cost model is not known. Default.                                       |

### cost_source values

| Value          | Meaning                                                       |
| -------------- | ------------------------------------------------------------- |
| `api_reported` | Token counts and/or cost came from the API response directly. |
| `log_parsed`   | Extracted from agent's stdout/stderr logs via regex.          |
| `calculated`   | Computed from token counts using a pricing table.             |
| `estimated`    | Best-effort estimate (e.g. from partial data).                |
| `unavailable`  | No cost data could be extracted. Default.                     |

### Error Hierarchy

```
Exception
  └── AdapterError           # base for all adapter errors
        ├── AdapterSetupError      # binary not found, auth missing, etc.
        └── AdapterExecutionError  # unrecoverable failure during agent run
```

Use `AdapterSetupError` for problems detected in `preflight()` or early in
`run()`. Use `AdapterExecutionError` for failures during execution (rate
limits, API errors). Both are caught by the eval runner, which records the
error and moves on.

## Pattern 1: CLI Adapter (BaseAdapter)

For agents invoked as a subprocess, extend `BaseAdapter` from
`src/codeprobe/adapters/_base.py`. It provides default implementations of
`preflight()` and `run()` -- you only need to implement `build_command()`.

### Minimal example

```python
from codeprobe.adapters._base import BaseAdapter
from codeprobe.adapters.protocol import AgentConfig

class MyAgentAdapter(BaseAdapter):
    _binary_name = "my-agent"
    _install_hint = "Install with: pip install my-agent"

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        binary = self._require_binary()
        cmd = [binary, "--prompt", prompt]
        if config.model:
            cmd.extend(["--model", config.model])
        return cmd
```

This gives you:

- **`preflight()`**: checks that `_binary_name` is on `PATH` via `shutil.which`.
- **`run()`**: calls `subprocess.run()` with timeout handling, catches
  `TimeoutExpired` and `FileNotFoundError`, and calls `parse_output()`.
- **`parse_output()`**: default implementation maps stdout/stderr/exit_code
  into `AgentOutput` with no token or cost data.

### Extracting tokens and cost

Override `parse_output()` to extract telemetry from the agent's output. Here
is how `AiderAdapter` parses cost data from stderr:

```python
import re
import subprocess
from codeprobe.adapters.protocol import AgentOutput

_TOKEN_RE = re.compile(r"Tokens:\s*([\d.]+k?)\s*sent,\s*([\d.]+k?)\s*received")
_COST_RE = re.compile(r"Cost:\s*\$([\d.]+)\s*message")

class AiderAdapter(BaseAdapter):
    # ... _binary_name, build_command ...

    def parse_output(
        self, result: subprocess.CompletedProcess[str], duration: float
    ) -> AgentOutput:
        combined = (result.stdout or "") + "\n" + (result.stderr or "")

        input_tokens = output_tokens = None
        cost_usd = None
        cost_model = "unknown"
        cost_source = "unavailable"

        token_match = _TOKEN_RE.search(combined)
        if token_match:
            input_tokens = _parse_token_value(token_match.group(1))
            output_tokens = _parse_token_value(token_match.group(2))

        cost_match = _COST_RE.search(combined)
        if cost_match:
            cost_usd = float(cost_match.group(1))
            cost_model = "per_token"
            cost_source = "log_parsed"

        return AgentOutput(
            stdout=result.stdout,
            stderr=result.stderr or None,
            exit_code=result.returncode,
            duration_seconds=duration,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            cost_model=cost_model,
            cost_source=cost_source,
        )
```

For agents that output structured JSON (like Claude Code with `--output-format json`),
use the `JsonStdoutCollector` or `ApiResponseCollector` from
`src/codeprobe/adapters/telemetry.py` instead of raw regex.

### Real CLI adapters

| Adapter          | File                                | Telemetry approach                    |
| ---------------- | ----------------------------------- | ------------------------------------- |
| `ClaudeAdapter`  | `src/codeprobe/adapters/claude.py`  | JSON stdout via `JsonStdoutCollector` |
| `CopilotAdapter` | `src/codeprobe/adapters/copilot.py` | NDJSON log parsing                    |
| `AiderAdapter`   | `src/codeprobe/adapters/aider.py`   | Regex on stderr                       |

## Pattern 2: API Adapter (direct SDK)

For agents accessed via a Python SDK, implement the Protocol directly without
`BaseAdapter`. There is no subprocess involved.

### Minimal example

```python
import os
import time
from codeprobe.adapters.protocol import (
    AdapterSetupError, AdapterExecutionError, AgentConfig, AgentOutput,
)

class MyApiAdapter:
    @property
    def name(self) -> str:
        return "my-api-agent"

    def preflight(self, config: AgentConfig) -> list[str]:
        issues: list[str] = []
        try:
            import my_sdk  # noqa: F401
        except ImportError:
            issues.append("my_sdk not installed. Run: pip install my-sdk")
            return issues
        if not os.environ.get("MY_API_KEY"):
            issues.append("MY_API_KEY environment variable not set")
        return issues

    def run(self, prompt: str, config: AgentConfig) -> AgentOutput:
        try:
            import my_sdk
        except ImportError:
            raise AdapterSetupError("my_sdk not installed")

        client = my_sdk.Client(api_key=os.environ["MY_API_KEY"])
        model = config.model or "default-model"
        start = time.monotonic()

        try:
            response = client.complete(model=model, prompt=prompt)
        except my_sdk.AuthError as exc:
            raise AdapterSetupError(f"Auth failed: {exc}") from exc
        except my_sdk.ApiError as exc:
            raise AdapterExecutionError(f"API error: {exc}") from exc

        duration = time.monotonic() - start

        return AgentOutput(
            stdout=response.text,
            stderr=None,
            exit_code=0,
            duration_seconds=duration,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cost_usd=response.usage.cost,
            cost_model="per_token",
            cost_source="api_reported",
        )
```

### Real API adapters

| Adapter               | File                                      | Notes                                                                                                                                  |
| --------------------- | ----------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `CodexAdapter`        | `src/codeprobe/adapters/codex.py`         | Tries Responses API, falls back to Chat Completions                                                                                    |
| `OpenAICompatAdapter` | `src/codeprobe/adapters/openai_compat.py` | Generic adapter for any OpenAI-compatible endpoint (Ollama, Together, vLLM, Groq, etc.) with configurable `base_url` and pricing table |

## Registration

Two places need to know about your adapter:

### 1. Entry points in pyproject.toml

Add your adapter to the `[project.entry-points."codeprobe.agents"]` table:

```toml
[project.entry-points."codeprobe.agents"]
aider   = "codeprobe.adapters.aider:AiderAdapter"
claude  = "codeprobe.adapters.claude:ClaudeAdapter"
codex   = "codeprobe.adapters.codex:CodexAdapter"
copilot = "codeprobe.adapters.copilot:CopilotAdapter"
openai  = "codeprobe.adapters.openai_compat:OpenAICompatAdapter"
myagent = "codeprobe.adapters.myagent:MyAgentAdapter"  # <-- add this
```

This lets the registry discover your adapter via `importlib.metadata.entry_points`.

### 2. \_BUILTINS in registry.py

For built-in adapters shipped with codeprobe, also add to the `_BUILTINS` dict
in `src/codeprobe/core/registry.py`:

```python
_BUILTINS: dict[str, str] = {
    "aider": "codeprobe.adapters.aider:AiderAdapter",
    "claude": "codeprobe.adapters.claude:ClaudeAdapter",
    "codex": "codeprobe.adapters.codex:CodexAdapter",
    "copilot": "codeprobe.adapters.copilot:CopilotAdapter",
    "myagent": "codeprobe.adapters.myagent:MyAgentAdapter",  # <-- add this
}
```

The registry checks `_BUILTINS` first (no installed package needed), then falls
back to entry points. This means third-party adapters only need the
`pyproject.toml` entry point -- they do not modify `_BUILTINS`.

### Third-party adapters

External packages can register adapters by adding an entry point in their own
`pyproject.toml`:

```toml
[project.entry-points."codeprobe.agents"]
myagent = "my_package.adapters:MyAgentAdapter"
```

After `pip install my-package`, `codeprobe run --agent myagent` picks it up
automatically.

## Testing

Tests live in `tests/test_adapters.py`. Follow the existing patterns:

### Protocol conformance

Verify your adapter satisfies the runtime-checkable Protocol:

```python
from codeprobe.adapters.protocol import AgentAdapter

def test_myagent_is_agent_adapter():
    adapter = MyAgentAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "myagent"
```

### Command building (CLI adapters)

Test `build_command()` with and without optional config:

```python
def test_myagent_build_command():
    adapter = MyAgentAdapter()
    config = AgentConfig(model="gpt-4")
    if adapter.find_binary():
        cmd = adapter.build_command("fix the bug", config)
        assert "--prompt" in cmd
        assert "fix the bug" in cmd
        assert "--model" in cmd
```

### Preflight checks

Test that missing prerequisites produce clear messages:

```python
def test_myagent_preflight_missing_binary():
    adapter = MyAgentAdapter()
    with patch.object(adapter, "find_binary", return_value=None):
        issues = adapter.preflight(AgentConfig())
        assert any("not found" in i.lower() for i in issues)
```

### Timeout and error handling (via BaseAdapter)

The existing `_StubAdapter` pattern in `tests/test_adapters.py` shows how to
test `BaseAdapter.run()` error paths using `unittest.mock.patch`:

```python
from unittest.mock import patch

class _StubAdapter(BaseAdapter):
    _binary_name = "fake-agent"
    _install_hint = "Install fake-agent"

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        return ["/usr/bin/fake-agent", "-p", prompt]

def test_timeout_returns_error_output():
    adapter = _StubAdapter()
    config = AgentConfig(timeout_seconds=5)
    exc = subprocess.TimeoutExpired(cmd=["fake-agent"], timeout=5)
    exc.stdout = "partial"
    exc.stderr = None
    with patch("subprocess.run", side_effect=exc):
        output = adapter.run("test", config)
    assert output.error is not None
    assert output.exit_code == -1
```

### AgentOutput validation

The framework enforces invariants at construction time. Test that your
adapter produces valid outputs:

```python
def test_myagent_output_valid_cost_model():
    # Ensure your adapter never sets an invalid cost_model
    output = MyAgentAdapter().run("test", AgentConfig())
    assert output.cost_model in ALLOWED_COST_MODELS
    assert output.cost_source in ALLOWED_COST_SOURCES
```

## Checklist

Before submitting a new adapter:

- [ ] Implements `name` (property), `preflight()`, and `run()`
- [ ] `preflight()` checks for binary/SDK and credentials
- [ ] `run()` extracts token counts and cost when available
- [ ] `cost_model` and `cost_source` are set honestly (never claim `api_reported` when parsing logs)
- [ ] Timeout is handled gracefully (BaseAdapter does this for CLI adapters)
- [ ] Errors raise `AdapterSetupError` or `AdapterExecutionError` (not bare exceptions)
- [ ] Added to `pyproject.toml` entry points
- [ ] Added to `_BUILTINS` in `registry.py` (if built-in)
- [ ] Tests: Protocol conformance, command building, preflight, output parsing
- [ ] Optional dependency added to `[project.optional-dependencies]` if needed
