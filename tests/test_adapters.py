"""Tests for agent adapter protocol and implementations."""

import subprocess
from unittest.mock import patch

import pytest

from codeprobe.adapters._base import BaseAdapter
from codeprobe.adapters.claude import ClaudeAdapter
from codeprobe.adapters.copilot import CopilotAdapter
from codeprobe.adapters.protocol import (
    ALLOWED_COST_MODELS,
    ALLOWED_COST_SOURCES,
    AdapterError,
    AdapterExecutionError,
    AdapterSetupError,
    AgentAdapter,
    AgentConfig,
    AgentOutput,
)


def test_claude_adapter_is_agent_adapter():
    adapter = ClaudeAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "claude"


def test_copilot_adapter_is_agent_adapter():
    adapter = CopilotAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "copilot"


def test_claude_build_command():
    adapter = ClaudeAdapter()
    config = AgentConfig(model="claude-sonnet-4-6")
    # Only test if claude binary exists
    if adapter.find_binary():
        cmd = adapter.build_command("fix the bug", config)
        assert "-p" in cmd
        assert "fix the bug" in cmd
        assert "--model" in cmd


def test_copilot_preflight_warns_on_mcp():
    adapter = CopilotAdapter()
    config = AgentConfig(mcp_config={"tools": ["search"]})
    issues = adapter.preflight(config)
    mcp_warnings = [i for i in issues if "MCP" in i]
    assert len(mcp_warnings) >= 1


def test_agent_output_is_frozen():
    output = AgentOutput(
        stdout="result",
        stderr=None,
        exit_code=0,
        duration_seconds=1.5,
    )
    assert output.stdout == "result"
    assert output.cost_usd is None


def test_agent_config_defaults():
    config = AgentConfig()
    assert config.permission_mode == "default"
    assert config.timeout_seconds == 300


def test_claude_permission_mode_passed():
    adapter = ClaudeAdapter()
    config = AgentConfig(permission_mode="plan")
    if adapter.find_binary():
        cmd = adapter.build_command("test", config)
        assert "--permission-mode" in cmd
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "plan"


def test_claude_default_permission_mode_omitted():
    adapter = ClaudeAdapter()
    config = AgentConfig(permission_mode="default")
    if adapter.find_binary():
        cmd = adapter.build_command("test", config)
        assert "--permission-mode" not in cmd


def test_claude_rejects_bypass_permissions():
    adapter = ClaudeAdapter()
    config = AgentConfig(permission_mode="bypassPermissions")
    if adapter.find_binary():
        with pytest.raises(ValueError, match="Unsafe permission_mode"):
            adapter.build_command("test", config)


# -- AgentOutput token/cost fields -------------------------------------------


class TestAgentOutputTokenFields:
    def test_default_token_fields_are_none(self) -> None:
        output = AgentOutput(stdout="ok", stderr=None, exit_code=0, duration_seconds=1.0)
        assert output.input_tokens is None
        assert output.output_tokens is None
        assert output.cache_read_tokens is None
        assert output.cost_model == "unknown"

    def test_per_token_cost_model(self) -> None:
        output = AgentOutput(
            stdout="ok",
            stderr=None,
            exit_code=0,
            duration_seconds=1.0,
            input_tokens=1000,
            output_tokens=500,
            cache_read_tokens=200,
            cost_model="per_token",
            cost_usd=0.05,
        )
        assert output.input_tokens == 1000
        assert output.output_tokens == 500
        assert output.cache_read_tokens == 200
        assert output.cost_model == "per_token"
        assert output.cost_usd == 0.05

    def test_subscription_cost_model(self) -> None:
        output = AgentOutput(
            stdout="ok",
            stderr=None,
            exit_code=0,
            duration_seconds=1.0,
            cost_model="subscription",
        )
        assert output.cost_model == "subscription"
        assert output.cost_usd is None

    def test_backward_compat_token_count(self) -> None:
        output = AgentOutput(
            stdout="ok",
            stderr=None,
            exit_code=0,
            duration_seconds=1.0,
            token_count=1500,
            cost_usd=0.03,
        )
        assert output.token_count == 1500
        assert output.cost_usd == 0.03

    def test_allowed_cost_models(self) -> None:
        assert "per_token" in ALLOWED_COST_MODELS
        assert "subscription" in ALLOWED_COST_MODELS
        assert "unknown" in ALLOWED_COST_MODELS


class TestAgentOutputValidation:
    def test_invalid_cost_model_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown cost_model"):
            AgentOutput(
                stdout="ok",
                stderr=None,
                exit_code=0,
                duration_seconds=1.0,
                cost_model="magic",
            )

    def test_per_token_without_cost_usd_raises(self) -> None:
        with pytest.raises(ValueError, match="cost_usd is required"):
            AgentOutput(
                stdout="ok",
                stderr=None,
                exit_code=0,
                duration_seconds=1.0,
                cost_model="per_token",
            )


# -- Narrowed Protocol --------------------------------------------------------


class TestNarrowedProtocol:
    def test_minimal_adapter_satisfies_protocol(self) -> None:
        """A class with only name/preflight/run satisfies AgentAdapter."""

        class MinimalAdapter:
            @property
            def name(self) -> str:
                return "minimal"

            def preflight(self, config: AgentConfig) -> list[str]:
                return []

            def run(self, prompt: str, config: AgentConfig) -> AgentOutput:
                return AgentOutput(
                    stdout="ok", stderr=None, exit_code=0, duration_seconds=0.1
                )

        adapter = MinimalAdapter()
        assert isinstance(adapter, AgentAdapter)
        assert adapter.name == "minimal"


# -- AgentOutput error / cost_source fields ------------------------------------


class TestAgentOutputErrorField:
    def test_default_is_none(self) -> None:
        output = AgentOutput(
            stdout="ok", stderr=None, exit_code=0, duration_seconds=1.0
        )
        assert output.error is None

    def test_can_set_error(self) -> None:
        output = AgentOutput(
            stdout="", stderr=None, exit_code=1, duration_seconds=1.0,
            error="Agent timed out after 300s",
        )
        assert output.error == "Agent timed out after 300s"


class TestAgentOutputCostSource:
    def test_default_is_unavailable(self) -> None:
        output = AgentOutput(
            stdout="ok", stderr=None, exit_code=0, duration_seconds=1.0
        )
        assert output.cost_source == "unavailable"

    @pytest.mark.parametrize("source", sorted(ALLOWED_COST_SOURCES))
    def test_valid_values_accepted(self, source: str) -> None:
        output = AgentOutput(
            stdout="ok", stderr=None, exit_code=0, duration_seconds=1.0,
            cost_source=source,
        )
        assert output.cost_source == source

    def test_invalid_cost_source_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown cost_source"):
            AgentOutput(
                stdout="ok", stderr=None, exit_code=0, duration_seconds=1.0,
                cost_source="magic",
            )


# -- AdapterError hierarchy ----------------------------------------------------


class TestAdapterErrorHierarchy:
    def test_all_subclasses_are_adapter_error(self) -> None:
        assert issubclass(AdapterSetupError, AdapterError)
        assert issubclass(AdapterExecutionError, AdapterError)

    def test_adapter_error_is_exception(self) -> None:
        assert issubclass(AdapterError, Exception)

    def test_can_catch_subclass_as_adapter_error(self) -> None:
        with pytest.raises(AdapterError):
            raise AdapterSetupError("binary missing")


# -- BaseAdapter run() error handling ------------------------------------------


class _StubAdapter(BaseAdapter):
    """Minimal concrete adapter for testing BaseAdapter.run()."""

    _binary_name = "fake-agent"
    _install_hint = "Install fake-agent"

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        return ["/usr/bin/fake-agent", "-p", prompt]


class TestBaseAdapterRunErrors:
    def test_timeout_returns_error_output(self) -> None:
        adapter = _StubAdapter()
        config = AgentConfig(timeout_seconds=5)
        exc = subprocess.TimeoutExpired(cmd=["fake-agent"], timeout=5)
        exc.stdout = "partial out"
        exc.stderr = "partial err"
        with patch("subprocess.run", side_effect=exc):
            output = adapter.run("test prompt", config)
        assert output.error == "Agent timed out after 5s"
        assert output.exit_code == -1
        assert output.stdout == "partial out"
        assert output.stderr == "partial err"

    def test_timeout_handles_none_output(self) -> None:
        adapter = _StubAdapter()
        config = AgentConfig(timeout_seconds=10)
        exc = subprocess.TimeoutExpired(cmd=["fake-agent"], timeout=10)
        exc.stdout = None
        exc.stderr = None
        with patch("subprocess.run", side_effect=exc):
            output = adapter.run("test", config)
        assert output.stdout == ""
        assert output.stderr is None
        assert output.error is not None

    def test_file_not_found_raises_setup_error(self) -> None:
        adapter = _StubAdapter()
        config = AgentConfig()
        with patch("subprocess.run", side_effect=FileNotFoundError("/usr/bin/fake-agent")):
            with pytest.raises(AdapterSetupError, match="Binary not found"):
                adapter.run("test", config)

    def test_require_binary_raises_setup_error(self) -> None:
        adapter = _StubAdapter()
        with patch.object(adapter, "find_binary", return_value=None):
            with pytest.raises(AdapterSetupError, match="CLI not found"):
                adapter._require_binary()


# -- BaseAdapter parse_output() ------------------------------------------------


class TestParseOutput:
    def test_default_maps_fields(self) -> None:
        adapter = _StubAdapter()
        result = subprocess.CompletedProcess(
            args=["fake-agent"], returncode=0, stdout="hello", stderr=""
        )
        output = adapter.parse_output(result, duration=2.5)
        assert output.stdout == "hello"
        assert output.stderr is None  # empty string → None
        assert output.exit_code == 0
        assert output.duration_seconds == 2.5

    def test_parse_failure_returns_output_with_error(self) -> None:
        class BrokenParser(BaseAdapter):
            _binary_name = "broken"
            _install_hint = "n/a"

            def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
                return ["broken"]

            def parse_output(
                self, result: subprocess.CompletedProcess[str], duration: float
            ) -> AgentOutput:
                raise ValueError("bad JSON")

        adapter = BrokenParser()
        config = AgentConfig()
        fake_result = subprocess.CompletedProcess(
            args=["broken"], returncode=0, stdout="raw output", stderr="err"
        )
        with patch("subprocess.run", return_value=fake_result):
            output = adapter.run("test", config)
        assert "Output parse failed" in output.error
        assert output.stdout == "raw output"
