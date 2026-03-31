"""Claude Code agent adapter."""

from __future__ import annotations

import shutil
import subprocess
import time

from codeprobe.adapters.protocol import AgentAdapter, AgentConfig, AgentOutput


class ClaudeAdapter:
    """Adapter for Claude Code CLI (claude -p)."""

    @property
    def name(self) -> str:
        return "claude"

    def find_binary(self) -> str | None:
        return shutil.which("claude")

    def preflight(self, config: AgentConfig) -> list[str]:
        issues: list[str] = []
        if self.find_binary() is None:
            issues.append("Claude CLI not found. Install from https://claude.ai/download")
        return issues

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        binary = self.find_binary()
        if binary is None:
            raise RuntimeError("Claude CLI not found")

        cmd = [binary, "-p", prompt, "--output-format", "json"]

        if config.model:
            cmd.extend(["--model", config.model])

        if config.permission_mode != "default":
            cmd.extend(["--permission-mode", config.permission_mode])

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
