"""GitHub Copilot CLI agent adapter."""

from __future__ import annotations

import shutil
import subprocess
import time

from codeprobe.adapters.protocol import AgentAdapter, AgentConfig, AgentOutput


class CopilotAdapter:
    """Adapter for GitHub Copilot CLI."""

    @property
    def name(self) -> str:
        return "copilot"

    def find_binary(self) -> str | None:
        return shutil.which("copilot")

    def preflight(self, config: AgentConfig) -> list[str]:
        issues: list[str] = []
        if self.find_binary() is None:
            issues.append("Copilot CLI not found. Install from https://github.com/github/copilot-cli")
        if config.mcp_config:
            issues.append("Copilot does not support MCP tools — mcp_config will be ignored")
        return issues

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        binary = self.find_binary()
        if binary is None:
            raise RuntimeError("Copilot CLI not found")

        cmd = [binary, "--prompt", prompt]

        if config.model:
            cmd.extend(["--model", config.model])

        return cmd

    def run(self, prompt: str, config: AgentConfig) -> AgentOutput:
        cmd = self.build_command(prompt, config)
        start = time.monotonic()

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
        )

        duration = time.monotonic() - start

        return AgentOutput(
            stdout=result.stdout,
            stderr=result.stderr or None,
            exit_code=result.returncode,
            duration_seconds=duration,
        )
