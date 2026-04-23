"""Multi-backend LLM utility for internal judgment calls.

This module provides LLM-as-judge functionality (e.g., ``codeprobe assess``,
``codeprobe mine --enrich``).  It is distinct from ``adapters/claude.py`` which
wraps Claude-as-eval-subject.

Backend priority (auto-detected):
  1. Anthropic Python SDK (``pip install codeprobe[anthropic]``) — cheapest for Haiku
  2. OpenAI Python SDK (``pip install codeprobe[codex]``) — GPT-4o-mini fallback
  3. Claude CLI (``claude`` binary on PATH)

Override with ``CODEPROBE_LLM_BACKEND=anthropic|openai|claude-cli``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from typing import Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base error for LLM operations."""


class LLMUnavailableError(LLMError):
    """No LLM backend available."""


class LLMExecutionError(LLMError):
    """LLM call failed (timeout, API error, non-zero exit)."""


class LLMParseError(LLMError):
    """Failed to parse LLM response."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

# Model aliases → canonical model IDs per backend.
#
# NOTE: These values must agree with ``src/codeprobe/llm/model_registry.yaml``
# which is the long-term source of truth for logical → backend id mapping.
# The ``opus``, ``sonnet`` and ``haiku`` short aliases exist in both stacks
# so legacy callers keep working; the registry holds date-less base slugs
# while this table keeps the dated variants for backwards compatibility
# with existing Anthropic deployments.
# Invariant enforced in tests/llm/test_registry.py:
# ``test_registry_opus_alias_matches_core_llm_constant``.
_ANTHROPIC_MODELS: dict[str, str] = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-6-20250514",
    # Opus 4.7 is the current production model. Unlike the other two
    # entries we use the base slug (no date suffix) because the dated
    # variant is not yet fixed in the Anthropic catalogue. Alias matches
    # the registry under tests/llm/test_registry.py::
    # test_registry_opus_alias_matches_core_llm_constant.
    "opus": "claude-opus-4-7",
}

_OPENAI_MODELS: dict[str, str] = {
    "haiku": "gpt-4o-mini",  # cheapest equivalent
    "sonnet": "gpt-4o",
    "opus": "gpt-4o",
}


@dataclass(frozen=True)
class LLMRequest:
    """Parameters for an LLM judgment call."""

    prompt: str
    model: str = "haiku"
    timeout_seconds: int = 30


@dataclass(frozen=True)
class LLMResponse:
    """Parsed response from an LLM call."""

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    model: str | None = None
    duration_ms: int | None = None
    backend: str | None = None


# ---------------------------------------------------------------------------
# Backend protocol
# ---------------------------------------------------------------------------


class LLMBackend(Protocol):
    """Interface for LLM backends."""

    name: str

    def available(self) -> bool:
        """Return True if this backend can be used."""
        ...

    def call(self, request: LLMRequest) -> LLMResponse:
        """Execute the request and return a response."""
        ...


# ---------------------------------------------------------------------------
# Backend: Anthropic Python SDK
# ---------------------------------------------------------------------------


class AnthropicSDKBackend:
    """Uses the ``anthropic`` Python package directly."""

    name = "anthropic"

    def available(self) -> bool:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def call(self, request: LLMRequest) -> LLMResponse:
        try:
            import anthropic
        except ImportError:
            raise LLMUnavailableError("anthropic SDK not installed")

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise LLMUnavailableError("ANTHROPIC_API_KEY not set")

        model_id = _ANTHROPIC_MODELS.get(request.model, request.model)
        client = anthropic.Anthropic(api_key=api_key)

        try:
            message = client.messages.create(
                model=model_id,
                max_tokens=4096,
                messages=[{"role": "user", "content": request.prompt}],
                timeout=request.timeout_seconds,
            )
        except Exception as exc:
            raise LLMExecutionError(f"Anthropic API error: {exc}") from exc

        text = ""
        for block in message.content:
            if hasattr(block, "text"):
                text += block.text

        return LLMResponse(
            text=text,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            model=model_id,
            backend="anthropic",
        )


# ---------------------------------------------------------------------------
# Backend: OpenAI Python SDK
# ---------------------------------------------------------------------------


class OpenAISDKBackend:
    """Uses the ``openai`` Python package (works with any OpenAI-compatible API)."""

    name = "openai"

    def available(self) -> bool:
        try:
            import openai  # noqa: F401
        except ImportError:
            return False
        return bool(os.environ.get("OPENAI_API_KEY"))

    def call(self, request: LLMRequest) -> LLMResponse:
        try:
            import openai
        except ImportError:
            raise LLMUnavailableError("openai SDK not installed")

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise LLMUnavailableError("OPENAI_API_KEY not set")

        model_id = _OPENAI_MODELS.get(request.model, request.model)
        client = openai.OpenAI(api_key=api_key)

        try:
            response = client.chat.completions.create(
                model=model_id,
                messages=[{"role": "user", "content": request.prompt}],
                timeout=request.timeout_seconds,
            )
        except Exception as exc:
            raise LLMExecutionError(f"OpenAI API error: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        text = choice.message.content if choice and choice.message else ""

        usage = response.usage
        return LLMResponse(
            text=text or "",
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            model=model_id,
            backend="openai",
        )


# ---------------------------------------------------------------------------
# Backend: Claude CLI
# ---------------------------------------------------------------------------


class ClaudeCLIBackend:
    """Shells out to ``claude -p --output-format json``."""

    name = "claude-cli"

    def available(self) -> bool:
        return shutil.which("claude") is not None

    def call(self, request: LLMRequest) -> LLMResponse:
        binary = shutil.which("claude")
        if binary is None:
            raise LLMUnavailableError("claude CLI not found on PATH")

        cmd = [
            binary,
            "-p",
            request.prompt,
            "--output-format",
            "json",
            "--model",
            request.model,
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMExecutionError(
                f"Claude CLI timed out after {request.timeout_seconds}s"
            ) from exc

        if result.returncode != 0:
            raise LLMExecutionError(
                f"Claude CLI exited with code {result.returncode}: "
                f"{(result.stderr or '').strip()[:200]}"
            )

        try:
            raw = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError) as exc:
            raise LLMParseError(f"Invalid JSON from Claude CLI: {exc}") from exc

        if not isinstance(raw, dict):
            raise LLMParseError(f"Expected JSON object, got {type(raw).__name__}")

        return replace(_parse_envelope(raw), model=request.model, backend="claude-cli")


# ---------------------------------------------------------------------------
# Envelope parser (Claude CLI specific)
# ---------------------------------------------------------------------------


def _parse_envelope(raw: dict[str, object]) -> LLMResponse:
    """Parse a raw JSON dict from Claude CLI into a validated ``LLMResponse``."""
    for key in ("type", "subtype", "is_error", "result"):
        if key not in raw:
            raise LLMParseError(f"Missing required envelope key: {key!r}")

    if raw.get("is_error"):
        raise LLMParseError(
            f"Claude returned an error envelope: {raw.get('result', '<no message>')}"
        )

    if raw.get("type") != "result":
        raise LLMParseError(
            f"Unexpected envelope type: {raw.get('type')!r} (expected 'result')"
        )

    usage = raw.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}

    duration_raw = raw.get("duration_ms")
    return LLMResponse(
        text=str(raw["result"]),
        input_tokens=(
            usage.get("input_tokens")
            if isinstance(usage.get("input_tokens"), int)
            else None
        ),
        output_tokens=(
            usage.get("output_tokens")
            if isinstance(usage.get("output_tokens"), int)
            else None
        ),
        cost_usd=(
            raw.get("total_cost_usd")
            if isinstance(raw.get("total_cost_usd"), (int, float))
            else None
        ),
        duration_ms=(
            int(duration_raw) if isinstance(duration_raw, (int, float)) else None
        ),
    )


# ---------------------------------------------------------------------------
# Backend resolution
# ---------------------------------------------------------------------------

_ALL_BACKENDS: list[LLMBackend] = [
    AnthropicSDKBackend(),
    OpenAISDKBackend(),
    ClaudeCLIBackend(),
]

_BACKEND_MAP: dict[str, LLMBackend] = {b.name: b for b in _ALL_BACKENDS}


def _resolve_backend() -> LLMBackend:
    """Pick the best available backend, respecting CODEPROBE_LLM_BACKEND override."""
    override = os.environ.get("CODEPROBE_LLM_BACKEND", "").strip()
    if override:
        backend = _BACKEND_MAP.get(override)
        if backend is None:
            raise LLMUnavailableError(
                f"Unknown backend {override!r}. "
                f"Choose from: {', '.join(_BACKEND_MAP)}"
            )
        if not backend.available():
            raise LLMUnavailableError(
                f"Backend {override!r} selected but not available. "
                f"Check dependencies and env vars."
            )
        return backend

    for backend in _ALL_BACKENDS:
        if backend.available():
            return backend

    raise LLMUnavailableError(
        "No LLM backend available. Install one of:\n"
        "  pip install codeprobe[anthropic]  # + set ANTHROPIC_API_KEY\n"
        "  pip install codeprobe[codex]      # + set OPENAI_API_KEY\n"
        "  Install Claude CLI               # + set ANTHROPIC_API_KEY"
    )


def llm_available() -> bool:
    """Return True if any LLM backend is available."""
    return any(b.available() for b in _ALL_BACKENDS)


# Keep old name for backward compatibility
claude_available = llm_available


def call_llm(request: LLMRequest) -> LLMResponse:
    """Route to the best available backend and return a response.

    Raises:
        LLMUnavailableError: No backend available.
        LLMExecutionError: Backend call failed.
        LLMParseError: Response parsing failed.
    """
    backend = _resolve_backend()
    logger.debug("Using LLM backend: %s", backend.name)
    return backend.call(request)


# Keep old name for backward compatibility
call_claude = call_llm
