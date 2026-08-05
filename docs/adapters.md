# Adapter Authoring Guide

This guide explains how to add support for a new AI coding agent to codeprobe.
Every mechanically checkable claim in it is enforced by
`tests/test_docs_adapters.py`: referenced paths must exist, the
`MinimalAdapter` example must actually satisfy the Protocol, and documented
defaults must match the code.

## Architecture Overview

codeprobe uses a Protocol-based adapter system. Every agent integration
implements the same five-member interface (`name`, `capabilities`,
`preflight`, `run`, `isolate_session`), which lets the eval runner treat all
agents identically regardless of whether they run as a CLI subprocess or hit
an HTTP API.

There are two common patterns:

| Pattern         | Base class                             | When to use                                                             |
| --------------- | -------------------------------------- | ----------------------------------------------------------------------- |
| **CLI adapter** | `BaseAdapter`                          | Agent is invoked via a subprocess (e.g. `claude -p`, `copilot --prompt`) |
| **API adapter** | None (implement the Protocol directly) | Agent is called via a Python SDK (e.g. the OpenAI client)                |

## The AgentAdapter Protocol

Defined in `src/codeprobe/adapters/protocol.py`:

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class AgentAdapter(Protocol):
    @property
    def name(self) -> str:
        """Human-readable agent name (e.g. 'claude', 'copilot')."""
        ...

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Which AgentConfig knobs this adapter honors. Fail-closed: a
        missing declaration is treated as prompt+model only."""
        ...

    def preflight(self, config: AgentConfig) -> list[str]:
        """Validate readiness. Return a list of issues (empty = ready)."""
        ...

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
        """Execute the agent and return results."""
        ...

    def isolate_session(self, slot_id: int) -> dict[str, str]:
        """Return per-slot env overrides for session isolation."""
        ...
```

Because `AgentAdapter` is a `@runtime_checkable` Protocol, you never need to
inherit from it. Any class with all five members satisfies the contract:

```python
from codeprobe.adapters.protocol import (
    AdapterCapabilities,
    AgentAdapter,
    AgentConfig,
    AgentOutput,
)

class MinimalAdapter:
    @property
    def name(self) -> str:
        return "my-agent"

    @property
    def capabilities(self) -> AdapterCapabilities:
        return AdapterCapabilities()  # prompt+model only

    def preflight(self, config: AgentConfig) -> list[str]:
        return []

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
        return AgentOutput(stdout="ok", stderr=None, exit_code=0, duration_seconds=0.1)

    def isolate_session(self, slot_id: int) -> dict[str, str]:
        return {}

assert isinstance(MinimalAdapter(), AgentAdapter)  # passes
```

`isinstance` only checks that the members exist, not their signatures. The
executor really does call `run(prompt, config, session_env=...)` and
`isolate_session(slot_id)` when running parallel slots (see
`src/codeprobe/core/executor.py`), so implement the full signatures rather
than the minimum that passes the check. Adapters whose agent keeps shared
session state on disk should return real per-slot overrides from
`isolate_session`; `ClaudeAdapter` mirrors the user config into a per-slot
`CLAUDE_CONFIG_DIR` for exactly this reason. Stateless adapters return `{}`.

### Capabilities

Adapters declare which `AgentConfig` knobs they actually honor via a
`capabilities` property returning `AdapterCapabilities` (defined in
`protocol.py`):

```python
@property
def capabilities(self) -> AdapterCapabilities:
    return AdapterCapabilities(mcp_config=True, workspace_cwd=True, timeout=True)
```

The contract is **fail-closed**: an adapter without the property is treated
as supporting nothing beyond prompt+model. `codeprobe run` checks every
experiment arm's requested knobs (`mcp_config`, resolved
`allowed_tools`/`disallowed_tools` — including the `mcp_mode`
strict/pragmatic auto-derived restriction — `max_turns`, and a declared
`permission_mode`) against the arm's adapter before any agent spawns, and
hard-refuses with `ADAPTER_CAPABILITY` on mismatch. There is no override
flag: a knob the adapter would silently drop makes the A/B labels lie.
Consequence: an MCP arm under the default `mcp_mode=strict` is refused on
adapters without tool-surface control (e.g. copilot); the honest path is
`mcp_mode=loose`, which runs with a comparison-validity warning.

For an auto-resolved Sourcegraph server named `sourcegraph`, both `strict`
and `pragmatic` also add `mcp__sourcegraph__evaluator` to the blocklist. The
tool executes arbitrary search scripts, and a non-interactive pilot showed
that an unbounded query can abort the agent stream. An explicit
`allowed_tools` or `disallowed_tools` list remains authoritative and skips
all automatic policy, including this guardrail; `loose` mode also leaves the
surface unrestricted by design.

Declarations must match the adapter's actual `config.*` usage — declare
what the code enforces today, not what the vendor CLI could support.

## Data Types

### AgentConfig

Configuration passed to every adapter method. All nine fields, in dataclass
order (`src/codeprobe/adapters/protocol.py`):

| Field              | Type                  | Default     | Notes                                                                                 |
| ------------------ | --------------------- | ----------- | ------------------------------------------------------------------------------------- |
| `model`            | `str \| None`         | `None`      | Model override (adapter picks its own default when `None`)                            |
| `permission_mode`  | `str`                 | `"default"` | One of `default`, `plan`, `auto`, `acceptEdits`, `dangerously_skip`                   |
| `timeout_seconds`  | `int`                 | `3600`      | Maximum execution time                                                                 |
| `mcp_config`       | `dict \| None`        | `None`      | MCP tool configuration (`BaseAdapter` writes it to a temp file, expanding exported `${VAR}` references and rejecting redacted or unresolved values) |
| `allowed_tools`    | `list[str] \| None`   | `None`      | Restrict the agent to these tools; `[]` disables all built-in tools (MCP-only arms)   |
| `disallowed_tools` | `list[str] \| None`   | `None`      | Tools the agent may not call                                                           |
| `extra`            | `dict \| None`        | `None`      | Adapter-specific options                                                               |
| `cwd`              | `str \| None`         | `None`      | Working directory for the agent                                                        |
| `max_turns`        | `int \| None`         | `None`      | Hard cap on agent turns; `None` = uncapped. See [agent_config.md](agent_config.md).   |

At execution time, a valid task-metadata `time_limit_sec` normally caps
`AgentConfig.timeout_seconds`; the effective value is the smaller of the two.
An explicit `codeprobe run --timeout N` is the highest-precedence operator
override and replaces both the task-metadata cap and
`experiment.json`'s `extra.timeout_seconds`. Without `--timeout`, the task
limit remains active, including when the experiment or built-in default is
larger.

`allowed_tools` and `disallowed_tools` are the primary knobs for
MCP/tool-config A/B experiments: when `allowed_tools` is an empty list the
adapter disables every built-in tool while MCP tools from `mcp_config` remain
available, so an arm can be measured on MCP tools alone.

### AgentOutput

Immutable dataclass returned by `run()`. All fields, in dataclass order:

| Field                   | Type                       | Default         | Notes                                                                                   |
| ----------------------- | -------------------------- | --------------- | ---------------------------------------------------------------------------------------- |
| `stdout`                | `str`                      | required        | Agent's primary output                                                                    |
| `stderr`                | `str \| None`              | required        | Standard error (or `None`)                                                                |
| `exit_code`             | `int`                      | required        | `0` = success, `-1` = timeout                                                             |
| `duration_seconds`      | `float`                    | required        | Wall-clock time                                                                           |
| `cost_usd`              | `float \| None`            | `None`          | Estimated cost in USD                                                                     |
| `input_tokens`          | `int \| None`              | `None`          | Input/prompt tokens                                                                       |
| `output_tokens`         | `int \| None`              | `None`          | Output/completion tokens                                                                  |
| `cache_read_tokens`     | `int \| None`              | `None`          | Prompt-cache hits                                                                         |
| `cache_creation_tokens` | `int \| None`              | `None`          | Prompt-cache writes                                                                       |
| `cost_model`            | `str`                      | `"unknown"`     | See [cost_model values](#cost_model-values)                                               |
| `error`                 | `str \| None`              | `None`          | Error description (partial results still preserved)                                       |
| `error_category`        | `str \| None`              | `None`          | Adapter-declared class (e.g. `"quota"`); `None` = executor default classification         |
| `error_terminal`        | `bool`                     | `False`         | Adapter-declared: `error` is a terminal agent outcome (e.g. turn cap), a genuine 0.0-reward measurement kept on checkpoint resume, not an infra casualty to retry. Only set for positively recognised stop conditions. |
| `cost_source`           | `str`                      | `"unavailable"` | See [cost_source values](#cost_source-values)                                             |
| `tool_call_count`       | `int \| None`              | `None`          | Number of `tool_use` blocks in agent output                                               |
| `tool_use_by_name`      | `dict[str, int] \| None`   | `None`          | Per-tool usage counts; `None` = not captured                                              |
| `num_turns`             | `int \| None`              | `None`          | From the CLI result record, when one exists                                               |
| `result_subtype`        | `str \| None`              | `None`          | Verbatim CLI result subtype (e.g. `"success"`, `"error_max_turns"`)                       |
| `duration_api_ms`       | `int \| None`              | `None`          | API-side duration from the CLI result record                                              |
| `mcp_init`              | `McpInitManifest \| None`  | `None`          | MCP surface declared in the init event and reconciled with later observed MCP calls; `None` when the adapter has no streaming transcript |

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
`preflight()`, `isolate_session()`, and `run()`; you only need to implement
`build_command()`.

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

- **`preflight()`**: checks that `_binary_name` is on `PATH` via `shutil.which`
  and reports `_install_hint` when it is missing.
- **`isolate_session()`**: returns `{}` (no per-slot isolation). Override if
  your agent keeps shared session state.
- **`run()`**: calls `subprocess.run()` with timeout handling, salvages partial
  output through `parse_output()` on timeout, cleans up the MCP config temp
  file, and catches `FileNotFoundError`. When `session_env` is provided, the
  child environment is rebuilt from an allow-list (`_ADAPTER_ENV_WHITELIST` in
  `src/codeprobe/adapters/_base.py`) so parent-process secrets never leak into
  the agent subprocess.
- **`parse_output()`**: default implementation maps stdout/stderr/exit_code
  into `AgentOutput` with no token or cost data.

### Extracting tokens and cost

Override `parse_output()` to extract telemetry from the agent's output. The
real reference implementation is `CopilotAdapter.parse_output` in
`src/codeprobe/adapters/copilot.py`: it feeds NDJSON stdout through
`NdjsonStreamCollector` (from `src/codeprobe/adapters/telemetry.py`), and when
NDJSON parsing fails it falls back to raw stdout while declaring the degraded
telemetry in the `error` field instead of hiding it.

For an agent that only prints usage to its logs, the shape looks like this.
This is a hypothetical example, not a real codeprobe adapter:

```python
import re
import subprocess

from codeprobe.adapters._base import BaseAdapter
from codeprobe.adapters.protocol import AgentOutput

_USAGE_RE = re.compile(r"tokens:\s*(\d+)\s*in\s*/\s*(\d+)\s*out", re.IGNORECASE)
_COST_RE = re.compile(r"cost:\s*\$([\d.]+)")

class LogScrapingAdapter(BaseAdapter):
    # ... _binary_name, _install_hint, build_command ...

    def parse_output(
        self, result: subprocess.CompletedProcess[str], duration: float
    ) -> AgentOutput:
        combined = (result.stdout or "") + "\n" + (result.stderr or "")

        input_tokens = output_tokens = None
        cost_usd = None
        cost_model = "unknown"
        cost_source = "unavailable"

        usage_match = _USAGE_RE.search(combined)
        if usage_match:
            input_tokens = int(usage_match.group(1))
            output_tokens = int(usage_match.group(2))

        cost_match = _COST_RE.search(combined)
        if cost_match:
            cost_usd = float(cost_match.group(1))
            cost_model = "per_token"
            cost_source = "log_parsed"  # honest: scraped from logs

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

For agents that emit a structured JSON envelope (like Claude Code with
`--output-format stream-json`), use `JsonStdoutCollector`; for direct API
responses, use `ApiResponseCollector`. Both live in
`src/codeprobe/adapters/telemetry.py`.

### Real CLI adapters

| Adapter          | File                                | Telemetry approach                                                                                     |
| ---------------- | ----------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `ClaudeAdapter`  | `src/codeprobe/adapters/claude.py`  | JSON envelope via `JsonStdoutCollector`; quota detection; per-slot `CLAUDE_CONFIG_DIR` session isolation |
| `CopilotAdapter` | `src/codeprobe/adapters/copilot.py` | NDJSON parsing via `NdjsonStreamCollector`                                                              |

## Pattern 2: API Adapter (direct SDK)

For agents accessed via a Python SDK, implement the Protocol directly without
`BaseAdapter`. There is no subprocess involved.

### Minimal example

```python
import os
import time

from codeprobe.adapters.protocol import (
    AdapterExecutionError,
    AdapterSetupError,
    AgentConfig,
    AgentOutput,
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

    def isolate_session(self, slot_id: int) -> dict[str, str]:
        return {}  # stateless API client: no per-slot state to isolate

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
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
| `OpenAICompatAdapter` | `src/codeprobe/adapters/openai_compat.py` | Building block for OpenAI-compatible endpoints (Ollama, Together, vLLM, Groq, etc.) with configurable `base_url` and pricing table. NOT registered; needs a no-arg wrapper (see [Registration](#registration)) |

### Quarantined: `CodexAdapter`

`CodexAdapter` (`src/codeprobe/adapters/codex.py`, `quarantined = True`) is
**not** a working comparison adapter. Its `run()` is a single-shot completion
call (Responses API, Chat Completions fallback): the model never sees the
workspace and cannot edit files, yet its `exit_code=0` outputs would read as
valid 0.0 measurements. `codeprobe run` refuses any codex arm upfront with
`ADAPTER_QUARANTINED` — before any arm runs or spends — and
`experiment add-config --agent codex` is rejected. The name stays registered
so the refusal is prescriptive rather than a raw `KeyError`. The quarantine
lifts when the adapter is rewritten around the real OpenAI Codex CLI agent
(workspace access, file edits, honest telemetry).

## Registration

The registry (`src/codeprobe/core/registry.py`) resolves `--agent <name>` in
two steps:

1. **`_BUILTINS`** in `src/codeprobe/core/registry.py`. This dict contains
   exactly the adapters shipped with codeprobe: `claude`, `codex`, and
   `copilot`. codeprobe's own `pyproject.toml` mirrors the same three names in
   `[project.entry-points."codeprobe.agents"]`.
2. **Entry points** in the `codeprobe.agents` group, discovered via
   `importlib.metadata.entry_points` across all installed packages.

In both cases the registry instantiates the class with a no-argument call,
`cls()`. **Your adapter class must be constructible with no arguments.** Read
endpoints, API keys, and default models from the environment or from
`AgentConfig` inside `__init__`, `preflight()`, or `run()`; never require
constructor parameters. An adapter whose constructor requires arguments will
raise `TypeError` at resolve time even though registration itself succeeds.

### Third-party adapters

External packages register adapters by adding an entry point in their own
`pyproject.toml`:

```toml
[project.entry-points."codeprobe.agents"]
myagent = "my_package.adapters:MyAgentAdapter"
```

After `pip install my-package`, `codeprobe run --agent myagent` picks it up
automatically. Do not edit codeprobe's `_BUILTINS` or `pyproject.toml`; those
list only the adapters shipped in this repository.

### Wrapping OpenAICompatAdapter

`OpenAICompatAdapter` (`src/codeprobe/adapters/openai_compat.py`) speaks the
OpenAI Chat Completions API and works with any compatible endpoint: Ollama,
Together, vLLM, Groq, and others. It is an unregistered building block, not a
usable `--agent` target: nothing references it in `_BUILTINS` or entry points,
and its constructor requires keyword-only `api_base` and `model` arguments, so
registering it directly would fail the registry's `cls()` call. Register a
thin no-arg wrapper instead:

```python
from codeprobe.adapters.openai_compat import OpenAICompatAdapter

class OllamaQwenAdapter(OpenAICompatAdapter):
    """No-arg wrapper so the registry's cls() instantiation works."""

    def __init__(self) -> None:
        super().__init__(
            api_base="http://localhost:11434/v1",
            model="qwen2.5-coder:32b",
            api_key_env="OLLAMA_API_KEY",
            adapter_name="ollama-qwen",
        )
```

```toml
[project.entry-points."codeprobe.agents"]
ollama-qwen = "my_package.adapters:OllamaQwenAdapter"
```

The optional `pricing` constructor argument takes a per-model
`{model: (input_per_1M, output_per_1M)}` table and enables per-token cost
calculation with `cost_source="calculated"`.

## Testing

Adapter tests in this repository live in `tests/test_adapters.py` (unit
patterns) and `tests/test_adapter_contracts.py` (parse contracts against real
transcripts). Follow the same patterns for a new adapter.

### Protocol conformance

Verify your adapter satisfies the runtime-checkable Protocol:

```python
from codeprobe.adapters.protocol import AgentAdapter

def test_myagent_is_agent_adapter():
    adapter = MyAgentAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "my-agent"
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

### Parse contracts against real transcripts

`tests/test_adapter_contracts.py` pins each adapter's `parse_output()` to real
agent transcripts stored under `tests/fixtures/` (e.g. `claude_normal.json`,
`claude_max_turns.json`, `copilot_no_tokens.txt`). Follow the
`Test<Agent>ParseContract` naming convention and cover at minimum:

- the happy path: tokens, cost, `cost_model`, and `cost_source` extracted;
- the telemetry-less transcript: `error` declared, `cost_usd=None`,
  `cost_source="unavailable"` (never fabricate telemetry);
- every terminal stop condition your adapter positively recognises
  (`error_terminal=True`) and proof that unrecognised errors stay
  `error_terminal=False`.

## Checklist

Before submitting a new adapter:

- [ ] Implements `name` (property), `preflight()`, `run(prompt, config, session_env=None)`, and `isolate_session(slot_id)`
- [ ] Class is no-arg constructible (the registry instantiates via `cls()`)
- [ ] Declares `capabilities` (`AdapterCapabilities`) matching actual `config.*` usage — undeclared knobs get the arm refused at preflight
- [ ] `preflight()` checks for binary/SDK and credentials
- [ ] `run()` extracts token counts and cost when available
- [ ] `cost_model` and `cost_source` are set honestly (never claim `api_reported` when parsing logs)
- [ ] If costs are computed from token counts (`cost_source="calculated"`), the rates come from a pricing table with a current `last_verified` date (see `src/codeprobe/adapters/pricing.py`)
- [ ] Quota/auth exhaustion is detected and reported with `error_category="quota"` so the executor can route it as an infra casualty (see `_detect_quota_error` in `src/codeprobe/adapters/claude.py`)
- [ ] `error_terminal=True` only for positively recognised terminal stop conditions (e.g. a turn cap); everything else stays `False` so it can be retried
- [ ] `isolate_session()` returns real per-slot env overrides when the agent keeps shared session state; `{}` otherwise
- [ ] Timeout is handled gracefully (BaseAdapter does this for CLI adapters)
- [ ] Errors raise `AdapterSetupError` or `AdapterExecutionError` (not bare exceptions)
- [ ] Entry point added in your package's `pyproject.toml` (`codeprobe.agents` group)
- [ ] Tests: Protocol conformance, command building, preflight, output parsing
- [ ] A `Test<Agent>ParseContract` class driven by real transcript fixtures under `tests/fixtures/`
- [ ] Optional dependency added to `[project.optional-dependencies]` if needed
