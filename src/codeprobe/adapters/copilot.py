"""GitHub Copilot CLI agent adapter."""

from __future__ import annotations

import json
import logging
import subprocess
import threading

from codeprobe.adapters._base import BaseAdapter
from codeprobe.adapters.protocol import (
    AdapterCapabilities,
    AgentConfig,
    AgentOutput,
)
from codeprobe.adapters.quota import detect_quota_error
from codeprobe.adapters.telemetry import NdjsonStreamCollector

logger = logging.getLogger(__name__)


def _literal_stdout_text(raw: str) -> str:
    """Return only the CLI's own literal (non-NDJSON-event) stdout lines.

    A quota/rate-limit stub is printed as bare text, while agent I/O is
    always emitted as NDJSON objects with a ``type`` field. Scanning the
    whole stdout would false-positive whenever the agent merely edits or
    reasons about code mentioning rate limits (same pitfall the Claude
    adapter's stream-json filter guards against, codeprobe-9tk).
    """
    literal: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            literal.append(line)
            continue
        if not isinstance(parsed, dict) or "type" not in parsed:
            literal.append(line)
    return "\n".join(literal)


class CopilotAdapter(BaseAdapter):
    """Adapter for GitHub Copilot CLI."""

    _binary_name = "copilot"
    _install_hint = (
        "Copilot CLI not found. Install from https://github.com/github/copilot-cli"
    )

    def __init__(self) -> None:
        self._collector = NdjsonStreamCollector()
        # Thread-local model context: the adapter instance is shared across
        # parallel slots, and build_command/parse_output for one task run on
        # the same worker thread inside BaseAdapter.run. build_command
        # records the selected model here so parse_output can price the
        # session with the matching rate card (mirrors ClaudeAdapter's
        # thread-local trace context).
        self._model_ctx: threading.local = threading.local()

    @property
    def capabilities(self) -> AdapterCapabilities:
        # Grep-verified: build_command consumes only model and mcp_config;
        # BaseAdapter.run enforces cwd and timeout_seconds. allowed_tools,
        # disallowed_tools, max_turns, and permission_mode have no CLI
        # mapping here (--allow-all-tools is unconditional), so they are
        # declared unsupported and run preflight refuses arms that set them.
        return AdapterCapabilities(
            mcp_config=True,
            workspace_cwd=True,
            timeout=True,
        )

    def preflight(self, config: AgentConfig) -> list[str]:
        return super().preflight(config)

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        binary = self._require_binary()
        cmd = [binary, "--prompt", prompt, "--output-format", "json"]

        # Non-interactive mode requires --allow-all-tools for tool auto-approval
        cmd.append("--allow-all-tools")

        # Record the model unconditionally: None must overwrite a stale
        # value left by a previous task on this worker thread.
        self._model_ctx.model = config.model
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
        usage = self._collector.collect(
            raw, model=getattr(self._model_ctx, "model", None)
        )

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
        except json.JSONDecodeError:
            logger.warning(
                "ndjson_parse_fallback: failed to parse Copilot NDJSON output, "
                "falling back to raw stdout"
            )
            result_text_parts = []
        stdout_text = "\n".join(result_text_parts) if result_text_parts else raw

        # When NDJSON parsing fell back to raw output, surface the fallback
        # in the error field so callers can detect degraded telemetry.
        error = usage.error
        if not result_text_parts and raw:
            fallback_msg = "ndjson_parse_fallback: raw stdout used as output"
            error = f"{error}; {fallback_msg}" if error else fallback_msg

        # Quota / rate-limit exhaustion prints as a literal line instead of
        # an NDJSON event. Stamp error_category='quota' so the executor
        # halts dispatch instead of burning the remaining trials, and
        # replace the telemetry error — a missing-outputTokens diagnosis is
        # a symptom of the quota stub, not a CLI version problem
        # (codeprobe-f7rl.29).
        error_category: str | None = None
        quota_line = detect_quota_error(_literal_stdout_text(raw), result.stderr)
        if quota_line is not None:
            error = f"quota/rate limit: {quota_line}"
            error_category = "quota"

        return AgentOutput(
            stdout=stdout_text,
            stderr=result.stderr or None,
            exit_code=result.returncode,
            duration_seconds=duration,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            cost_model=usage.cost_model,
            cost_source=usage.cost_source,
            error=error,
            error_category=error_category,
        )
