"""GitHub Copilot CLI agent adapter."""

from __future__ import annotations

import json
import logging
import subprocess

from codeprobe.adapters._base import BaseAdapter
from codeprobe.adapters.protocol import AgentConfig, AgentOutput

logger = logging.getLogger(__name__)


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
        cmd = [binary, "--prompt", prompt, "--output-format", "json"]

        if config.model:
            cmd.extend(["--model", config.model])

        return cmd

    def parse_output(
        self, result: subprocess.CompletedProcess[str], duration: float
    ) -> AgentOutput:
        """Parse Copilot CLI NDJSON output for token data.

        Requires Copilot CLI 1.0.4+ with --output-format json which emits
        NDJSON lines containing "assistant.message" events with outputTokens.
        Raises an error if structured token data is not present.
        """
        raw = result.stdout or ""
        output_tokens = None
        result_text_parts: list[str] = []

        try:
            for line in raw.strip().splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                event_type = obj.get("type", "")
                if event_type == "assistant.message":
                    data = obj.get("data", {})
                    tokens = data.get("outputTokens")
                    if tokens is not None:
                        output_tokens = tokens
                    content = data.get("content", "")
                    if content:
                        result_text_parts.append(content)
                elif event_type == "result":
                    content = obj.get("data", {}).get("content", "")
                    if content:
                        result_text_parts.append(content)
        except (json.JSONDecodeError, ValueError):
            return AgentOutput(
                stdout=raw,
                stderr=result.stderr or None,
                exit_code=result.returncode,
                duration_seconds=duration,
                error=(
                    "Copilot CLI did not return structured JSON. "
                    "codeprobe requires Copilot CLI >= 1.0.4 with --output-format json support. "
                    "Upgrade with: gh extension upgrade copilot"
                ),
            )

        stdout_text = "\n".join(result_text_parts) if result_text_parts else raw

        if output_tokens is None:
            return AgentOutput(
                stdout=stdout_text,
                stderr=result.stderr or None,
                exit_code=result.returncode,
                duration_seconds=duration,
                error=(
                    "Copilot CLI returned JSON but no outputTokens field. "
                    "Ensure Copilot CLI >= 1.0.4. Upgrade with: gh extension upgrade copilot"
                ),
            )

        return AgentOutput(
            stdout=stdout_text,
            stderr=result.stderr or None,
            exit_code=result.returncode,
            duration_seconds=duration,
            output_tokens=output_tokens,
            cost_model="subscription",
            cost_source="api_reported",
        )
