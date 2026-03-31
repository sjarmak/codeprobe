"""Claude Code agent adapter."""

from __future__ import annotations

import json
import logging
import subprocess

from codeprobe.adapters._base import BaseAdapter
from codeprobe.adapters.protocol import ALLOWED_PERMISSION_MODES, AgentConfig, AgentOutput

logger = logging.getLogger(__name__)


class ClaudeAdapter(BaseAdapter):
    """Adapter for Claude Code CLI (claude -p)."""

    _binary_name = "claude"
    _install_hint = "Claude CLI not found. Install from https://claude.ai/download"

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        binary = self._require_binary()
        cmd = [binary, "-p", prompt, "--output-format", "json"]

        if config.model:
            cmd.extend(["--model", config.model])

        if config.permission_mode != "default":
            if config.permission_mode not in ALLOWED_PERMISSION_MODES:
                raise ValueError(
                    f"Unsafe permission_mode: {config.permission_mode!r}. "
                    f"Allowed: {', '.join(sorted(ALLOWED_PERMISSION_MODES))}"
                )
            cmd.extend(["--permission-mode", config.permission_mode])

        return cmd

    def parse_output(
        self, result: subprocess.CompletedProcess[str], duration: float
    ) -> AgentOutput:
        """Parse Claude CLI JSON envelope into AgentOutput."""
        try:
            envelope = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning("Failed to parse Claude JSON output: %s", exc)
            return AgentOutput(
                stdout=result.stdout,
                stderr=result.stderr or None,
                exit_code=result.returncode,
                duration_seconds=duration,
                error=f"JSON parse failed: {exc}",
            )

        stdout_text = envelope.get("result", result.stdout)
        usage = envelope.get("usage")
        cost_usd_raw = envelope.get("total_cost_usd")

        if usage is None:
            return AgentOutput(
                stdout=stdout_text,
                stderr=result.stderr or None,
                exit_code=result.returncode,
                duration_seconds=duration,
                error="Missing usage block in Claude output",
            )

        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        cache_read_tokens = usage.get("cache_read_input_tokens")

        # per_token requires cost_usd to be non-None
        if cost_usd_raw is not None:
            cost_model = "per_token"
            cost_source = "api_reported"
        else:
            logger.warning("Claude output has usage block but no total_cost_usd")
            cost_model = "unknown"
            cost_source = "unavailable"

        return AgentOutput(
            stdout=stdout_text,
            stderr=result.stderr or None,
            exit_code=result.returncode,
            duration_seconds=duration,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost_usd_raw,
            cost_model=cost_model,
            cost_source=cost_source,
        )
