"""GitHub Copilot CLI agent adapter."""

from __future__ import annotations

from codeprobe.adapters._base import BaseAdapter
from codeprobe.adapters.protocol import AgentConfig


class CopilotAdapter(BaseAdapter):
    """Adapter for GitHub Copilot CLI."""

    _binary_name = "copilot"
    _install_hint = "Copilot CLI not found. Install from https://github.com/github/copilot-cli"

    def preflight(self, config: AgentConfig) -> list[str]:
        issues = super().preflight(config)
        if config.mcp_config:
            issues.append("Copilot does not support MCP tools — mcp_config will be ignored")
        return issues

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        binary = self._require_binary()
        cmd = [binary, "--prompt", prompt]

        if config.model:
            cmd.extend(["--model", config.model])

        return cmd
