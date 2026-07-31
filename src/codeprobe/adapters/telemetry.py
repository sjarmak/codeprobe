"""Telemetry collection — standalone token/cost extraction from agent output."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from codeprobe.adapters.pricing import (
    CLAUDE_PRICING,
    CODEX_PRICING,
    COPILOT_PRICING,
    PricingTable,
    strip_model_date_suffix,
)
from codeprobe.adapters.protocol import (
    ALLOWED_COST_MODELS,
    ALLOWED_COST_SOURCES,
    McpInitManifest,
    McpServerStatus,
)

logger = logging.getLogger(__name__)

__all__ = [
    "CLAUDE_PRICING",
    "CODEX_PRICING",
    "COPILOT_PRICING",
    "ApiResponseCollector",
    "JsonStdoutCollector",
    "NdjsonStreamCollector",
    "TelemetryCollector",
    "UsageData",
]


@dataclass(frozen=True)
class UsageData:
    """Telemetry data extracted from agent output.

    Mirrors the token/cost fields of ``AgentOutput`` but is standalone —
    no stdout/stderr/duration baggage.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_creation_tokens: int | None = None
    cost_usd: float | None = None
    cost_model: str = "unknown"
    cost_source: str = "unavailable"
    error: str | None = None
    tool_call_count: int | None = None
    # Tool-use counts broken down by tool name (e.g. ``{"Read": 5,
    # "mcp__sourcegraph__keyword_search": 2}``). Populated only when the
    # adapter captured a streaming transcript. None means "not captured",
    # not "no tool calls".
    tool_use_by_name: dict[str, int] | None = None
    # Result-record fields extracted verbatim from the CLI's terminal
    # envelope (codeprobe-8up). None when the output had no parseable
    # result record (quota stubs, malformed output).
    num_turns: int | None = None
    result_subtype: str | None = None
    duration_api_ms: int | None = None

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


def _envelope_field(envelope: dict[str, Any], key: str, typ: type) -> Any:
    """Return ``envelope[key]`` when it is an instance of ``typ``, else None.

    Mechanical structural guard for verbatim result-record extraction —
    wrong-typed values are dropped, never coerced.
    """
    value = envelope.get(key)
    return value if isinstance(value, typ) else None


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


@dataclass(frozen=True)
class _StreamUsage:
    """Per-turn ``assistant`` usage summed across a stream-json transcript.

    A stream that dies before its terminal ``result`` event (timeout kill)
    still carries the tokens the API actually billed on each assistant
    event's ``message.usage``. ``models`` holds the distinct
    ``message.model`` values seen on usage-bearing turns, in stream order;
    ``turns`` counts those usage-bearing turns.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    models: tuple[str, ...] = ()
    turns: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )


def _usage_int(usage: dict[str, Any], key: str) -> int:
    """Return ``usage[key]`` when it is an int, else 0 — never coerced."""
    value = usage.get(key)
    return value if isinstance(value, int) else 0


def _parse_stream_json(
    raw_output: str,
) -> tuple[dict[str, Any] | None, int, dict[str, int], _StreamUsage]:
    """Parse a ``--output-format stream-json --verbose`` transcript.

    Returns ``(result_event, tool_use_count, tool_use_by_name,
    stream_usage)``. ``result_event`` is the final ``type: "result"`` event
    (same shape as ``--output-format json`` envelope), or None when the
    stream is malformed or has no terminal event. ``tool_use_by_name``
    aggregates tool-use block counts by tool name (including MCP tools,
    which appear as ``mcp__<server>__<tool>``), useful for observability.
    ``stream_usage`` sums ``message.usage`` across assistant events so a
    truncated stream still accounts for billed tokens.
    """
    result_event: dict[str, Any] | None = None
    tool_use_count = 0
    by_name: dict[str, int] = {}
    input_sum = output_sum = cache_read_sum = cache_creation_sum = 0
    usage_turns = 0
    models: list[str] = []
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant":
            msg = ev.get("message")
            if isinstance(msg, dict):
                for block in msg.get("content", []) or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        tool_use_count += 1
                        name = block.get("name", "")
                        if isinstance(name, str) and name:
                            by_name[name] = by_name.get(name, 0) + 1
                usage = msg.get("usage")
                if isinstance(usage, dict):
                    usage_turns += 1
                    input_sum += _usage_int(usage, "input_tokens")
                    output_sum += _usage_int(usage, "output_tokens")
                    cache_read_sum += _usage_int(usage, "cache_read_input_tokens")
                    cache_creation_sum += _usage_int(
                        usage, "cache_creation_input_tokens"
                    )
                    model = msg.get("model")
                    if isinstance(model, str) and model and model not in models:
                        models.append(model)
        if ev.get("type") == "result":
            result_event = ev
    stream_usage = _StreamUsage(
        input_tokens=input_sum,
        output_tokens=output_sum,
        cache_read_tokens=cache_read_sum,
        cache_creation_tokens=cache_creation_sum,
        models=tuple(models),
        turns=usage_turns,
    )
    return result_event, tool_use_count, by_name, stream_usage


def _recover_truncated_stream_usage(
    stream_usage: _StreamUsage,
    *,
    tool_call_count: int | None,
    tool_use_by_name: dict[str, int] | None,
) -> UsageData | None:
    """Rebuild telemetry for a stream that died before its ``result`` event.

    A timed-out (or killed) agent stream carries real billed usage on its
    per-turn assistant events even though the terminal envelope never
    arrived. Sum that usage and price it from :data:`CLAUDE_PRICING` so the
    spend counts toward the run budget with honest provenance
    (``cost_source='calculated'`` — never ``api_reported``). Returns None
    when the stream carried no usage at all; the caller keeps its
    parse-failure UsageData (codeprobe-f7rl.34).

    Cost requires exactly one known model across the usage-bearing turns.
    Otherwise tokens are preserved but cost stays None with
    ``cost_source='unavailable'`` — a missing figure, never a wrong-rate
    guess.
    """
    if stream_usage.turns == 0 or stream_usage.total_tokens == 0:
        return None

    error = (
        "stream ended without a result event; usage summed from "
        f"{stream_usage.turns} assistant turns"
    )
    rate: tuple[float, ...] | None = None
    if len(stream_usage.models) == 1:
        rate = CLAUDE_PRICING.rates.get(
            strip_model_date_suffix(stream_usage.models[0])
        )
    if rate is None:
        logger.warning(
            "Truncated stream usage recovered but no single known model "
            "(saw %r); tokens preserved, cost unavailable",
            stream_usage.models,
        )
        return UsageData(
            input_tokens=stream_usage.input_tokens,
            output_tokens=stream_usage.output_tokens,
            cache_read_tokens=stream_usage.cache_read_tokens,
            cache_creation_tokens=stream_usage.cache_creation_tokens,
            error=error,
            tool_call_count=tool_call_count,
            tool_use_by_name=tool_use_by_name,
        )

    input_rate, output_rate, cache_read_rate, cache_creation_rate = rate
    cost_usd = (
        stream_usage.input_tokens * input_rate
        + stream_usage.output_tokens * output_rate
        + stream_usage.cache_read_tokens * cache_read_rate
        + stream_usage.cache_creation_tokens * cache_creation_rate
    ) / 1_000_000
    return UsageData(
        input_tokens=stream_usage.input_tokens,
        output_tokens=stream_usage.output_tokens,
        cache_read_tokens=stream_usage.cache_read_tokens,
        cache_creation_tokens=stream_usage.cache_creation_tokens,
        cost_usd=cost_usd,
        cost_model="per_token",
        cost_source="calculated",
        error=error,
        tool_call_count=tool_call_count,
        tool_use_by_name=tool_use_by_name,
    )


def parse_mcp_init_manifest(raw_output: str) -> McpInitManifest:
    """Reconcile the init snapshot with MCP tools observed later in the stream.

    The Claude CLI run with ``--output-format stream-json --verbose`` emits
    a ``type: "system"`` / ``subtype: "init"`` event before the first turn
    that lists the ``tools`` offered (built-in + ``mcp__<server>__<tool>``)
    and the ``mcp_servers`` it attached, each with a ``status``. HTTP servers
    may attach after this event, so later MCP ``tool_use`` blocks are retained
    as stronger evidence that a server contributed a callable tool.

    Returns a captured manifest when the init event is present; otherwise a
    ``McpInitManifest(captured=False)`` — never None — so callers record an
    explicit "not measured" rather than silently dropping the surface.
    """
    _, _, tools_by_name, _ = _parse_stream_json(raw_output)
    observed_tools = tuple(
        tool for tool in tools_by_name if tool.startswith("mcp__")
    )
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(ev, dict) or ev.get("type") != "system":
            continue
        # First system event carrying the init surface wins. Some CLI
        # versions tag it ``subtype: "init"``; tolerate the field's absence
        # as long as the surface keys are present. Requiring at least one
        # surface key stops a bare ``{"type": "system"}`` event from
        # matching first and shadowing a real init event later in the
        # stream with an empty ``captured=True`` manifest.
        if ev.get("subtype") not in (None, "init"):
            continue
        if "tools" not in ev and "mcp_servers" not in ev:
            continue
        raw_tools = ev.get("tools")
        tools: tuple[str, ...] = tuple(
            t for t in raw_tools if isinstance(t, str)
        ) if isinstance(raw_tools, list) else ()
        raw_servers = ev.get("mcp_servers")
        servers: tuple[McpServerStatus, ...] = (
            tuple(
                McpServerStatus(
                    name=str(s.get("name", "")),
                    status=str(s.get("status", "")),
                )
                for s in raw_servers
                if isinstance(s, dict)
            )
            if isinstance(raw_servers, list)
            else ()
        )
        return McpInitManifest(
            captured=True,
            offered_tools=tools,
            observed_tools=observed_tools,
            mcp_servers=servers,
        )
    return McpInitManifest(
        captured=False,
        observed_tools=observed_tools,
    )


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
        # Two accepted shapes:
        #   1. ``--output-format json`` — a single JSON envelope; no
        #      per-tool-use trace, so tool_call_count stays None.
        #   2. ``--output-format stream-json --verbose`` — newline-delimited
        #      events ending in a ``type: "result"`` event that mirrors
        #      shape (1). We also count ``tool_use`` blocks across all
        #      ``assistant`` events for accurate tool_call_count.
        #
        # When ``context`` carries a ``trace_recorder``, ``trace_config``,
        # and ``trace_task_id`` triple, we replay the stream into the
        # recorder so R5's trace.db gets populated at the same parse
        # step that feeds ``UsageData`` — keeps the transcript and the
        # telemetry in sync by construction.
        stream_tool_count: int | None = None
        stream_tool_by_name: dict[str, int] = {}
        trace_recorder = context.get("trace_recorder")
        trace_config = context.get("trace_config")
        trace_task_id = context.get("trace_task_id")
        if (
            trace_recorder is not None
            and trace_config is not None
            and trace_task_id is not None
        ):
            try:
                trace_recorder.ingest_stream(
                    raw_output,
                    config=str(trace_config),
                    task_id=str(trace_task_id),
                )
            except Exception:  # noqa: BLE001 — trace must not break telemetry
                # Best-effort by contract: a trace.db failure (locked
                # sqlite, full disk) must not be laundered into an
                # "Output parse failed" AgentOutput that loses the
                # agent's actual result and telemetry.
                logger.exception(
                    "Trace recorder failed to ingest stream for config=%s task=%s",
                    trace_config,
                    trace_task_id,
                )
        trimmed = raw_output.lstrip()
        if trimmed.startswith("{\n") or trimmed.startswith("{"):
            # Try single-envelope path first — most adapters still use
            # ``--output-format json``.
            try:
                envelope = json.loads(raw_output)
                if envelope.get("type") == "result" and "\n" in raw_output.rstrip():
                    # Ambiguous: looks like a single-line event from the
                    # stream. Fall through to stream parsing below.
                    raise ValueError("ambiguous envelope — retry as stream")
            except (json.JSONDecodeError, ValueError):
                envelope = None
        else:
            envelope = None
        if envelope is None:
            (
                result_ev,
                stream_tool_count,
                stream_tool_by_name,
                stream_usage,
            ) = _parse_stream_json(raw_output)
            if result_ev is None:
                # Truncated stream (timeout kill): recover the billed
                # usage from per-turn assistant events so the spend still
                # reaches the budget and reports (codeprobe-f7rl.34).
                recovered = _recover_truncated_stream_usage(
                    stream_usage,
                    tool_call_count=stream_tool_count,
                    tool_use_by_name=stream_tool_by_name or None,
                )
                if recovered is not None:
                    return recovered
                return UsageData(
                    error="JSON parse failed: output is neither a valid "
                    "envelope nor a stream-json transcript ending in a "
                    "'result' event"
                )
            envelope = result_ev

        usage = envelope.get("usage")
        if usage is None:
            return UsageData(error="Missing usage block in Claude output")

        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        cache_read_tokens = usage.get("cache_read_input_tokens")
        cache_creation_tokens = usage.get("cache_creation_input_tokens")
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

        # Prefer stream-json count when the transcript was streamed — it's
        # always present and accurate. Fall back to the envelope's
        # ``messages`` array (when some future CLI flag surfaces it), else
        # stays None.
        tool_call_count = stream_tool_count
        if tool_call_count is None:
            tool_call_count = _count_tool_use_blocks(envelope)

        return UsageData(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cost_usd=cost_usd_raw,
            cost_model=cost_model,
            cost_source=cost_source,
            tool_call_count=tool_call_count,
            tool_use_by_name=stream_tool_by_name or None,
            error=envelope_error,
            num_turns=_envelope_field(envelope, "num_turns", int),
            result_subtype=_envelope_field(envelope, "subtype", str),
            duration_api_ms=_envelope_field(envelope, "duration_api_ms", int),
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

    Accepts ``model`` via ``**context``: the Copilot model the session was
    launched with, used to select the rate card. ``None`` means the session
    default (assumed gpt-4o).
    """

    def collect(self, raw_output: str, **context: Any) -> UsageData:
        model: str | None = context.get("model")
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
                    "Either the CLI is older than 1.0.4 (upgrade with: "
                    "gh extension upgrade copilot) or the session failed "
                    "before producing output (auth, quota, or rate-limit "
                    "exhaustion)."
                ),
            )

        # Estimate input tokens from stream content if not natively reported.
        # Try tiktoken first for exact counts, then fall back to heuristic.
        heuristic_used = False
        if input_tokens is None and input_text_parts:
            combined_text = " ".join(input_text_parts)
            tiktoken_count = _count_tokens_tiktoken(combined_text, "gpt-4o")
            if tiktoken_count is not None:
                input_tokens = tiktoken_count
                logger.debug(
                    "Copilot input_tokens=%d counted via tiktoken from %d stream chars",
                    input_tokens,
                    input_chars,
                )
            elif input_chars > 0:
                # ~4 chars/token (mirrors _estimate_tokens) computed from the
                # length directly — no need to materialize a throwaway string.
                input_tokens = max(1, input_chars // 4)
                heuristic_used = True
                logger.debug(
                    "Copilot input_tokens=%d estimated from %d stream chars",
                    input_tokens,
                    input_chars,
                )

        # Cost from token counts using the rate card for the model the
        # session was launched with; gpt-4o is the documented default-session
        # assumption when no model was requested. A model missing from
        # COPILOT_PRICING gets no cost at all — an honest gap, never a
        # wrong-rate-card guess.
        requested_model = model or "gpt-4o"
        rates = COPILOT_PRICING.rates.get(requested_model)
        estimated_cost: float | None = None
        if rates is None:
            logger.warning(
                "No Copilot pricing for model %r (rate card covers: %s); "
                "cost_usd unavailable",
                requested_model,
                ", ".join(sorted(COPILOT_PRICING.rates)),
            )
        else:
            out_cost = output_tokens * rates[1] / 1_000_000
            in_cost = (
                input_tokens * rates[0] / 1_000_000
                if input_tokens is not None
                else 0.0
            )
            estimated_cost = in_cost + out_cost

        # Label by how the numbers were produced: 'estimated' only when the
        # chars//4 heuristic supplied input_tokens; native counts and
        # tiktoken-exact counts are 'calculated'.
        if estimated_cost is not None:
            cost_source = "estimated" if heuristic_used else "calculated"
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

    def __init__(
        self, pricing: PricingTable | dict[str, tuple[float, float]] | None = None
    ) -> None:
        source = pricing if pricing is not None else CODEX_PRICING
        # The collector only needs the model→rate mapping, so normalize a
        # PricingTable down to its rates dict and keep a plain-dict lookup.
        self._rates = source.rates if isinstance(source, PricingTable) else source

    def collect(self, raw_output: str, **context: Any) -> UsageData:
        input_tokens: int | None = context.get("input_tokens")
        output_tokens: int | None = context.get("output_tokens")
        model: str = context.get("model", "")

        if input_tokens is None or output_tokens is None:
            return UsageData(error="OpenAI response contained no usage data")

        pricing = self._rates.get(model)
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
