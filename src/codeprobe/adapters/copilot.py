"""GitHub Copilot CLI agent adapter."""

from __future__ import annotations

import glob
import json
import logging
import os
import subprocess

from codeprobe.adapters._base import BaseAdapter
from codeprobe.adapters.protocol import AgentConfig, AgentOutput
from codeprobe.adapters.telemetry import NdjsonStreamCollector

logger = logging.getLogger(__name__)

# Common Copilot CLI log paths
_COPILOT_LOG_PATHS = [
    os.path.expanduser("~/.copilot/logs"),
    os.path.expanduser("~/.config/github-copilot/logs"),
]


class CopilotAdapter(BaseAdapter):
    """Adapter for GitHub Copilot CLI."""

    _binary_name = "copilot"
    _install_hint = (
        "Copilot CLI not found. Install from https://github.com/github/copilot-cli"
    )

    def __init__(self) -> None:
        self._collector = NdjsonStreamCollector()

    def preflight(self, config: AgentConfig) -> list[str]:
        return super().preflight(config)

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        binary = self._require_binary()
        cmd = [binary, "--prompt", prompt, "--output-format", "json"]

        # Non-interactive mode requires --allow-all-tools for tool auto-approval
        cmd.append("--allow-all-tools")

        if config.model:
            cmd.extend(["--model", config.model])

        mcp_path = self._write_mcp_config(config)
        if mcp_path:
            cmd.extend(["--additional-mcp-config", f"@{mcp_path}"])

        return cmd

    def parse_output(
        self, result: subprocess.CompletedProcess[str], duration: float
    ) -> AgentOutput:
        """Parse Copilot CLI NDJSON output for token data.

        Requires Copilot CLI 1.0.4+ with --output-format json which emits
        NDJSON lines containing "assistant.message" events with outputTokens.

        Input tokens are extracted from NDJSON ``usage`` events when available,
        falling back to Copilot process log parsing.
        """
        raw = result.stdout or ""
        usage = self._collector.collect(raw)

        # Extract content text from NDJSON events.
        # On JSON parse failure, the except clause resets to empty,
        # and the fallback below uses raw output — matching original behavior.
        result_text_parts: list[str] = []
        try:
            for line in raw.strip().splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                event_type = obj.get("type", "")
                if event_type == "assistant.message":
                    content = obj.get("data", {}).get("content", "")
                    if content:
                        result_text_parts.append(content)
                elif event_type == "result":
                    content = obj.get("data", {}).get("content", "")
                    if content:
                        result_text_parts.append(content)
        except (json.JSONDecodeError, ValueError):
            result_text_parts = []
        stdout_text = "\n".join(result_text_parts) if result_text_parts else raw

        input_tokens = usage.input_tokens
        cost_source = usage.cost_source

        # Fall back to process log parsing for input tokens
        if input_tokens is None:
            log_tokens = self._extract_tokens_from_logs()
            if log_tokens is not None:
                input_tokens = log_tokens
                cost_source = "log_parsed"
                logger.debug(
                    "Copilot input_tokens=%d extracted from process log",
                    log_tokens,
                )
            else:
                logger.debug(
                    "Copilot input_tokens unavailable "
                    "(no NDJSON usage, no process logs)"
                )

        return AgentOutput(
            stdout=stdout_text,
            stderr=result.stderr or None,
            exit_code=result.returncode,
            duration_seconds=duration,
            input_tokens=input_tokens,
            output_tokens=usage.output_tokens,
            cost_model=usage.cost_model,
            cost_source=cost_source,
            error=usage.error,
        )

    def _extract_tokens_from_logs(self) -> int | None:
        """Extract input token count from the most recent Copilot process log.

        Returns the last seen input_tokens value (most complete snapshot),
        or None if logs are unavailable or unparseable.
        """
        for log_dir in _COPILOT_LOG_PATHS:
            if not os.path.isdir(log_dir):
                continue
            log_files = sorted(
                glob.glob(os.path.join(log_dir, "process-*.log")),
                key=os.path.getmtime,
                reverse=True,
            )
            if not log_files:
                continue
            # Guard against symlinks pointing outside the log directory
            log_path = os.path.realpath(log_files[0])
            if not log_path.startswith(os.path.realpath(log_dir)):
                continue
            try:
                best: int | None = None
                with open(log_path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            obj = json.loads(line)
                            log_usage = obj.get("usage", {})
                            prompt_tokens = log_usage.get(
                                "prompt_tokens"
                            ) or log_usage.get("inputTokens")
                            if prompt_tokens is not None:
                                best = int(prompt_tokens)
                        except (json.JSONDecodeError, ValueError):
                            continue
                if best is not None:
                    return best
            except OSError:
                continue
        return None
