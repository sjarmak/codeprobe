"""Aider CLI agent adapter."""

from __future__ import annotations

import re
import subprocess

from codeprobe.adapters._base import BaseAdapter
from codeprobe.adapters.protocol import AgentConfig, AgentOutput

# Matches: "Tokens: 1.2k sent, 856 received. Cost: $0.0034 message, $0.0034 session."
# Token counts may be plain integers or k-suffixed floats (e.g., 45.3k).
_TOKEN_RE = re.compile(r"Tokens:\s*([\d.]+k?)\s*sent,\s*([\d.]+k?)\s*received")
_COST_RE = re.compile(r"Cost:\s*\$([\d.]+)\s*message")


def _parse_token_value(raw: str) -> int:
    """Parse a token count string like '1.2k' or '856' into an integer."""
    raw = raw.strip().lower()
    if raw.endswith("k"):
        return int(float(raw[:-1]) * 1000)
    return int(float(raw))


class AiderAdapter(BaseAdapter):
    """Adapter for Aider CLI (aider --message)."""

    _binary_name = "aider"
    _install_hint = "Aider CLI not found. Install with: pip install aider-chat"

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        binary = self._require_binary()
        cmd = [binary, "--message", prompt, "--yes-always", "--no-git"]

        if config.model:
            cmd.extend(["--model", config.model])

        return cmd

    def parse_output(
        self, result: subprocess.CompletedProcess[str], duration: float
    ) -> AgentOutput:
        """Parse Aider CLI output for token counts and cost.

        Aider prints a summary line like:
            Tokens: 1.2k sent, 856 received. Cost: $0.0034 message, $0.0034 session.

        This may appear in stdout or stderr. We search both.
        """
        combined = (result.stdout or "") + "\n" + (result.stderr or "")

        input_tokens: int | None = None
        output_tokens: int | None = None
        cost_usd: float | None = None
        cost_model = "unknown"
        cost_source = "unavailable"

        token_match = _TOKEN_RE.search(combined)
        if token_match:
            input_tokens = _parse_token_value(token_match.group(1))
            output_tokens = _parse_token_value(token_match.group(2))

        cost_match = _COST_RE.search(combined)
        if cost_match:
            cost_usd = float(cost_match.group(1))
            cost_model = "per_token"
            cost_source = "log_parsed"

        return AgentOutput(
            stdout=result.stdout,
            stderr=result.stderr or None,
            exit_code=result.returncode,
            duration_seconds=duration,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            cost_model=cost_model,
            cost_source=cost_source,
        )
