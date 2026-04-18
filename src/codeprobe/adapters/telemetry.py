"""Telemetry collection — standalone token/cost extraction from agent output."""

from __future__ import annotations

import json
import logging
import warnings
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol, runtime_checkable

from codeprobe.adapters.protocol import ALLOWED_COST_MODELS, ALLOWED_COST_SOURCES

logger = logging.getLogger(__name__)

# Date when pricing tables were last verified against vendor pages.
# If today is >90 days past this date, a warning is emitted at import time.
_PRICING_LAST_VERIFIED: date = date(2026, 4, 2)

_PRICING_STALENESS_DAYS = 90

if (date.today() - _PRICING_LAST_VERIFIED).days > _PRICING_STALENESS_DAYS:
    warnings.warn(
        f"Pricing tables were last verified on {_PRICING_LAST_VERIFIED}. "
        f"They may be outdated (>{_PRICING_STALENESS_DAYS} days). "
        "Update _PRICING_LAST_VERIFIED after re-checking vendor pricing pages.",
        UserWarning,
        stacklevel=1,
    )

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

# Copilot pricing per 1M tokens: (input, output)
# GPT-4o rates — Copilot's underlying model for code tasks.
COPILOT_PRICING: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
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
    tool_call_count: int | None = None

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


def _extract_envelope_error(envelope: dict[str, Any]) -> str | None:
    """Return an error message if the Claude CLI envelope signals failure.

    The CLI produces structured JSON even for failures: auth errors, API
    errors, and turn-limit hits set ``is_error=true`` and/or populate
    ``api_error_status`` / ``subtype=error_*``.  Returns a short message
    drawn from ``result`` when an error is detected, else ``None``.
    """
    # `is True` rejects the JSON-decoded `"true"` string and integer `1` —
    # only a genuine JSON boolean should flip the error flag.
    is_error = envelope.get("is_error") is True
    api_error_status = envelope.get("api_error_status")
    # Only treat api_error_status as an error signal when it is an HTTP error
    # code (>=400). Some CLI versions emit 0 or null for success, and a future
    # 2xx sentinel must not trip the error path.
    api_status_is_error = isinstance(api_error_status, int) and api_error_status >= 400
    subtype = envelope.get("subtype")
    subtype_is_error = isinstance(subtype, str) and subtype.startswith("error_")

    if not (is_error or api_status_is_error or subtype_is_error):
        return None

    result_msg = envelope.get("result")
    if isinstance(result_msg, str) and result_msg.strip():
        return result_msg.strip()

    parts: list[str] = []
    if subtype_is_error:
        parts.append(f"subtype={subtype}")
    if api_status_is_error:
        parts.append(f"api_error_status={api_error_status}")
    if not parts:
        parts.append("is_error=true")
    return "Claude CLI reported error (" + ", ".join(parts) + ")"


def _count_tool_use_blocks(envelope: dict[str, Any]) -> int | None:
    """Count ``tool_use`` content blocks in a Claude CLI JSON envelope.

    Iterates the ``messages`` array (when present) and counts content
    blocks with ``type == "tool_use"`` in assistant messages.
    Returns ``None`` when the envelope has no ``messages`` key.
    """
    messages = envelope.get("messages")
    if messages is None:
        return None

    count = 0
    for msg in messages:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                count += 1
    return count


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

        # Detect error envelopes from the Claude CLI. Auth failures, API errors,
        # and max_turns hits come back as structured JSON with is_error=true or
        # a non-null api_error_status, but still include (often zeroed) usage
        # and cost blocks — so we can't rely on missing fields to signal error.
        # When a run errors without doing meaningful work (zero tokens), clear
        # cost fields so downstream never reports a misleading "api_reported /
        # $0" row for a run that never invoked the model.  max_turns and
        # similar mid-run failures preserve real cost/token data.
        envelope_error = _extract_envelope_error(envelope)
        ran_work = bool((input_tokens or 0) + (output_tokens or 0))

        if cost_usd_raw is not None and (envelope_error is None or ran_work):
            cost_model = "per_token"
            cost_source = "api_reported"
        else:
            if cost_usd_raw is None:
                logger.warning("Claude output has usage block but no total_cost_usd")
            cost_usd_raw = None
            cost_model = "unknown"
            cost_source = "unavailable"

        tool_call_count = _count_tool_use_blocks(envelope)

        return UsageData(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost_usd_raw,
            cost_model=cost_model,
            cost_source=cost_source,
            tool_call_count=tool_call_count,
            error=envelope_error,
        )


def _count_tokens_tiktoken(text: str, model: str) -> int | None:
    """Count tokens using tiktoken if available.

    Returns the exact token count, or ``None`` if tiktoken is not installed
    or the model encoding cannot be resolved.
    """
    try:
        import tiktoken  # noqa: F811
    except ImportError:
        return None

    try:
        enc = tiktoken.encoding_for_model(model)
    except KeyError:
        # Unknown model — fall back to cl100k_base (GPT-4 family default)
        try:
            enc = tiktoken.get_encoding("cl100k_base")
        except Exception:
            return None

    try:
        return len(enc.encode(text))
    except Exception:
        return None


def _estimate_tokens(text: str) -> int:
    """Estimate token count from text using a ~4 chars/token heuristic.

    This is a conservative estimate for Claude/GPT tokenizers.
    Production systems should use tiktoken for exact counts.
    """
    return max(1, len(text) // 4) if text else 0


class NdjsonStreamCollector:
    """Extract telemetry from Copilot CLI NDJSON stream on stdout.

    Iterates NDJSON lines to extract:
    - ``outputTokens`` from ``assistant.message`` events
    - Input tokens estimated from ``user.message`` and ``tool.execution_complete``
      content (Copilot CLI does not report input tokens natively)
    """

    def collect(self, raw_output: str, **context: Any) -> UsageData:
        raw = raw_output or ""
        output_tokens = None
        input_tokens = None
        input_chars = 0  # accumulate input content for estimation
        input_text_parts: list[str] = []  # accumulate raw text for tiktoken

        try:
            for line in raw.strip().splitlines():
                if not line.strip():
                    continue
                obj = json.loads(line)
                event_type = obj.get("type", "")
                data = obj.get("data", {})

                if event_type == "assistant.message":
                    out_tok = data.get("outputTokens")
                    if out_tok is not None:
                        if output_tokens is None:
                            output_tokens = out_tok
                        else:
                            output_tokens += out_tok
                    # Count assistant content as input for subsequent turns
                    content = data.get("content", "")
                    if content:
                        input_chars += len(content)
                        input_text_parts.append(content)
                elif event_type == "usage":
                    in_tok = data.get("inputTokens")
                    if in_tok is not None:
                        input_tokens = in_tok
                    # usage event outputTokens is a summary total — only use
                    # it if no output_tokens collected from assistant.message
                    out_tok = data.get("outputTokens")
                    if out_tok is not None and output_tokens is None:
                        output_tokens = out_tok
                elif event_type == "user.message":
                    content = data.get("transformedContent") or data.get("content", "")
                    input_chars += len(content)
                    if content:
                        input_text_parts.append(content)
                elif event_type == "tool.execution_complete":
                    result = data.get("result", {})
                    content = result.get("detailedContent") or result.get("content", "")
                    input_chars += len(content)
                    if content:
                        input_text_parts.append(content)
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

        # Estimate input tokens from stream content if not natively reported.
        # Try tiktoken first for exact counts, then fall back to heuristic.
        tiktoken_used = False
        if input_tokens is None and input_text_parts:
            combined_text = " ".join(input_text_parts)
            tiktoken_count = _count_tokens_tiktoken(combined_text, "gpt-4o")
            if tiktoken_count is not None:
                input_tokens = tiktoken_count
                tiktoken_used = True
                logger.debug(
                    "Copilot input_tokens=%d counted via tiktoken from %d stream chars",
                    input_tokens,
                    input_chars,
                )
            elif input_chars > 0:
                input_tokens = _estimate_tokens(
                    "x" * input_chars  # placeholder, only length matters
                )
                logger.debug(
                    "Copilot input_tokens=%d estimated from %d stream chars",
                    input_tokens,
                    input_chars,
                )

        # Estimate cost from token counts using GPT-4o pricing (Copilot's
        # underlying model).  Even on a subscription plan, token-based cost
        # estimates allow meaningful comparisons across configs and agents.
        estimated_cost: float | None = None
        gpt4o_pricing = COPILOT_PRICING.get("gpt-4o")
        if gpt4o_pricing is not None and output_tokens is not None:
            out_cost = output_tokens * gpt4o_pricing[1] / 1_000_000
            in_cost = (
                input_tokens * gpt4o_pricing[0] / 1_000_000
                if input_tokens is not None
                else 0.0
            )
            estimated_cost = in_cost + out_cost

        # When tiktoken provides exact input counts, cost_source is 'calculated'.
        # When using heuristic estimation, cost_source is 'estimated'.
        if estimated_cost is not None:
            cost_source = "calculated" if tiktoken_used else "estimated"
        else:
            cost_source = "unavailable"

        return UsageData(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=estimated_cost,
            cost_model="per_token" if estimated_cost is not None else "subscription",
            cost_source=cost_source,
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
