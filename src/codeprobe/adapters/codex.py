"""OpenAI Codex agent adapter — API-based (no CLI subprocess)."""

from __future__ import annotations

import logging
import os
import time

from codeprobe.adapters.protocol import (
    AdapterExecutionError,
    AdapterSetupError,
    AgentConfig,
    AgentOutput,
)
from codeprobe.adapters.telemetry import ApiResponseCollector

logger = logging.getLogger(__name__)


class CodexAdapter:
    """Adapter for OpenAI Codex API (responses.create)."""

    def __init__(self) -> None:
        self._collector = ApiResponseCollector()

    @property
    def name(self) -> str:
        return "codex"

    def preflight(self, config: AgentConfig) -> list[str]:
        issues: list[str] = []
        try:
            import openai  # noqa: F401
        except ImportError:
            issues.append(
                "openai SDK not found. Install with: pip install codeprobe[codex]"
            )
            return issues
        if not os.environ.get("OPENAI_API_KEY"):
            issues.append("OPENAI_API_KEY environment variable not set")
        return issues

    def run(self, prompt: str, config: AgentConfig) -> AgentOutput:
        try:
            import openai
        except ImportError:
            raise AdapterSetupError(
                "openai SDK not installed. Run: pip install codeprobe[codex]"
            )

        client = openai.OpenAI()
        model = config.model or "codex-mini-latest"
        start = time.monotonic()

        try:
            response = client.responses.create(model=model, input=prompt)
        except openai.AuthenticationError as exc:
            raise AdapterSetupError(f"OPENAI_API_KEY invalid: {exc}") from exc
        except openai.RateLimitError as exc:
            raise AdapterExecutionError(f"Rate limited: {exc}") from exc
        except openai.APIError as exc:
            raise AdapterExecutionError(f"OpenAI API error: {exc}") from exc

        duration = time.monotonic() - start
        stdout = response.output_text or ""

        # Collect telemetry via the collector
        input_tokens = getattr(response.usage, "input_tokens", None) if response.usage else None
        output_tokens = getattr(response.usage, "output_tokens", None) if response.usage else None

        usage = self._collector.collect(
            stdout,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
        )

        return AgentOutput(
            stdout=stdout,
            stderr=None,
            exit_code=0,
            duration_seconds=duration,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            cost_model=usage.cost_model,
            cost_source=usage.cost_source,
            error=usage.error,
        )
