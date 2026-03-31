"""Tests for agent adapter protocol and implementations."""

from codeprobe.adapters.protocol import AgentAdapter, AgentConfig, AgentOutput
from codeprobe.adapters.claude import ClaudeAdapter
from codeprobe.adapters.copilot import CopilotAdapter


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
    import pytest
    adapter = ClaudeAdapter()
    config = AgentConfig(permission_mode="bypassPermissions")
    if adapter.find_binary():
        with pytest.raises(ValueError, match="Unsafe permission_mode"):
            adapter.build_command("test", config)
