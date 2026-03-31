"""Tests for agent adapter protocol and implementations."""

import pytest

from codeprobe.adapters.claude import ClaudeAdapter
from codeprobe.adapters.copilot import CopilotAdapter
from codeprobe.adapters.protocol import (
    ALLOWED_COST_MODELS,
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
