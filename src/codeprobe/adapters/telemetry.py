"""Telemetry collection — standalone token/cost extraction from agent output."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from codeprobe.adapters.protocol import ALLOWED_COST_MODELS, ALLOWED_COST_SOURCES

logger = logging.getLogger(__name__)

# Pricing per 1M tokens: (input, output)
CODEX_PRICING: dict[str, tuple[float, float]] = {
    "codex-mini-latest": (1.50, 6.00),
    "codex-latest": (2.00, 8.00),
}

# Claude pricing per 1M tokens: (input, output, cache_read, cache_creation)
# Cache creation is billed at 1.25x the input rate.
CLAUDE_PRICING: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-4-6": (15.00, 75.00, 1.50, 18.75),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30, 3.75),
    "claude-haiku-4-5": (0.80, 4.00, 0.08, 1.00),
}


@dataclass(frozen=True)
class UsageData:
    """Telemetry data extracted from agent output.

    Mirrors the token/cost fields of ``AgentOutput`` but is standalone —
    no stdout/stderr/duration baggage.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_usd: float | None = None
    cost_model: str = "unknown"
    cost_source: str = "unavailable"
    error: str | None = None

    def __post_init__(self) -> None:
        if self.cost_model not in ALLOWED_COST_MODELS:
            raise ValueError(
                f"Invalid cost_model: {self.cost_model!r}. "
                f"Expected one of: {sorted(ALLOWED_COST_MODELS)}"
            )
        if self.cost_source not in ALLOWED_COST_SOURCES:
            raise ValueError(
                f"Invalid cost_source: {self.cost_source!r}. "
                f"Expected one of: {sorted(ALLOWED_COST_SOURCES)}"
            )


@runtime_checkable
class TelemetryCollector(Protocol):
    """Protocol for extracting telemetry from raw agent output."""

    def collect(self, raw_output: str, **context: Any) -> UsageData: ...


class JsonStdoutCollector:
    """Extract telemetry from Claude CLI JSON envelope on stdout.

    Expected shape::

        {
            "result": "...",
            "usage": {
                "input_tokens": 12345,
                "output_tokens": 6789,
                "cache_read_input_tokens": 1000
            },
            "total_cost_usd": 0.0423
        }
    """

    def collect(self, raw_output: str, **context: Any) -> UsageData:
        try:
            envelope = json.loads(raw_output)
        except (json.JSONDecodeError, ValueError) as exc:
            return UsageData(error=f"JSON parse failed: {exc}")

        usage = envelope.get("usage")
        if usage is None:
            return UsageData(error="Missing usage block in Claude output")

        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        cache_read_tokens = usage.get("cache_read_input_tokens")
        cost_usd_raw = envelope.get("total_cost_usd")

        if cost_usd_raw is not None:
            cost_model = "per_token"
            cost_source = "api_reported"
        else:
            logger.warning("Claude output has usage block but no total_cost_usd")
            cost_model = "unknown"
            cost_source = "unavailable"

        return UsageData(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost_usd_raw,
            cost_model=cost_model,
            cost_source=cost_source,
        )


class NdjsonStreamCollector:
    """Extract telemetry from Copilot CLI NDJSON stream on stdout.

    Iterates NDJSON lines looking for ``assistant.message`` events
    with an ``outputTokens`` field. Always returns
    ``cost_model="subscription"``, ``cost_source="api_reported"``.
    """

    def collect(self, raw_output: str, **context: Any) -> UsageData:
        raw = raw_output or ""
        output_tokens = None
        input_tokens = None

        try:
            for line in raw.strip().splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                event_type = obj.get("type", "")
                if event_type in ("usage", "assistant.message"):
                    data = obj.get("data", {})
                    in_tok = data.get("inputTokens")
                    if in_tok is not None:
                        input_tokens = in_tok
                    out_tok = data.get("outputTokens")
                    if out_tok is not None:
                        output_tokens = out_tok
                elif event_type == "result":
                    usage = obj.get("usage", {})
                    in_tok = usage.get("inputTokens")
                    if in_tok is None:
                        in_tok = usage.get("prompt_tokens")
                    if in_tok is not None and input_tokens is None:
                        input_tokens = in_tok
                    out_tok = usage.get("outputTokens")
                    if out_tok is None:
                        out_tok = usage.get("completion_tokens")
                    if out_tok is not None and output_tokens is None:
                        output_tokens = out_tok
        except (json.JSONDecodeError, ValueError):
            return UsageData(
                error=(
                    "Copilot CLI did not return structured JSON. "
                    "codeprobe requires Copilot CLI >= 1.0.4 with "
                    "--output-format json support. "
                    "Upgrade with: gh extension upgrade copilot"
                ),
            )

        if output_tokens is None:
            return UsageData(
                error=(
                    "Copilot CLI returned JSON but no outputTokens field. "
                    "Ensure Copilot CLI >= 1.0.4. "
                    "Upgrade with: gh extension upgrade copilot"
                ),
            )

        return UsageData(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_model="subscription",
            cost_source="api_reported",
        )


class ApiResponseCollector:
    """Extract telemetry from pre-parsed API response data.

    Expects ``input_tokens``, ``output_tokens``, and ``model`` passed
    via ``**context``.  Calculates cost from a pricing table.
    """

    def __init__(self, pricing: dict[str, tuple[float, float]] | None = None) -> None:
        self._pricing = pricing if pricing is not None else CODEX_PRICING

    def collect(self, raw_output: str, **context: Any) -> UsageData:
        input_tokens: int | None = context.get("input_tokens")
        output_tokens: int | None = context.get("output_tokens")
        model: str = context.get("model", "")

        if input_tokens is None or output_tokens is None:
            return UsageData(error="OpenAI response contained no usage data")

        pricing = self._pricing.get(model)
        if pricing is not None:
            cost_usd = (
                input_tokens * pricing[0] / 1_000_000
                + output_tokens * pricing[1] / 1_000_000
            )
            cost_model = "per_token"
            cost_source = "calculated"
        else:
            logger.warning("No pricing data for model %r; cost_usd unavailable", model)
            cost_usd = None
            cost_model = "unknown"
            cost_source = "unavailable"

        return UsageData(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            cost_model=cost_model,
            cost_source=cost_source,
        )
