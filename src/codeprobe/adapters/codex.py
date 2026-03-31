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

logger = logging.getLogger(__name__)

# Pricing per 1M tokens: (input, output)
_PRICING: dict[str, tuple[float, float]] = {
    "codex-mini-latest": (1.50, 6.00),
}


class CodexAdapter:
    """Adapter for OpenAI Codex API (responses.create)."""

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

        if response.usage is None:
            return AgentOutput(
                stdout=stdout,
                stderr=None,
                exit_code=0,
                duration_seconds=duration,
                error="OpenAI response contained no usage data",
            )

        input_tokens = response.usage.input_tokens
        output_tokens = response.usage.output_tokens
        model_name = config.model or "codex-mini-latest"
        pricing = _PRICING.get(model_name)

        if pricing is not None:
            cost_usd = (
                input_tokens * pricing[0] / 1_000_000
                + output_tokens * pricing[1] / 1_000_000
            )
            cost_model = "per_token"
            cost_source = "calculated"
        else:
            logger.warning("No pricing data for model %r; cost_usd unavailable", model_name)
            cost_usd = None
            cost_model = "unknown"
            cost_source = "unavailable"

        return AgentOutput(
            stdout=stdout,
            stderr=None,
            exit_code=0,
            duration_seconds=duration,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            cost_model=cost_model,
            cost_source=cost_source,
        )
