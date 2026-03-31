"""Shared Claude CLI utility for internal judgment calls.

This module provides Claude-as-judge functionality (e.g., for ``codeprobe assess``).
It is distinct from ``adapters/claude.py`` which wraps Claude-as-eval-subject.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, replace

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class LLMError(Exception):
    """Base error for LLM operations."""


class LLMUnavailableError(LLMError):
    """Claude CLI binary not found on PATH."""


class LLMExecutionError(LLMError):
    """Claude CLI subprocess failed (timeout, non-zero exit)."""


class LLMParseError(LLMError):
    """Failed to parse Claude CLI JSON output or envelope schema mismatch."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMRequest:
    """Parameters for a Claude CLI judgment call."""

    prompt: str
    model: str = "haiku"
    timeout_seconds: int = 30


@dataclass(frozen=True)
class LLMResponse:
    """Parsed response from a Claude CLI call."""

    text: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    model: str | None = None
    duration_ms: int | None = None


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


def claude_available() -> bool:
    """Return True if the ``claude`` CLI binary is on PATH."""
    return shutil.which("claude") is not None


def _parse_envelope(raw: dict[str, object]) -> LLMResponse:
    """Parse a raw JSON dict into a validated ``LLMResponse``.

    Raises ``LLMParseError`` on missing keys, error envelopes, or type
    mismatches.
    """
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
        input_tokens=usage.get("input_tokens") if isinstance(usage.get("input_tokens"), int) else None,
        output_tokens=usage.get("output_tokens") if isinstance(usage.get("output_tokens"), int) else None,
        cost_usd=raw.get("total_cost_usd") if isinstance(raw.get("total_cost_usd"), (int, float)) else None,
        duration_ms=int(duration_raw) if isinstance(duration_raw, (int, float)) else None,
    )


def call_claude(request: LLMRequest) -> LLMResponse:
    """Shell out to ``claude -p --output-format json`` and return a parsed response.

    Raises:
        LLMUnavailableError: Claude CLI not on PATH.
        LLMExecutionError: Subprocess timeout or non-zero exit.
        LLMParseError: Invalid JSON or envelope schema mismatch.
    """
    binary = shutil.which("claude")
    if binary is None:
        raise LLMUnavailableError("claude CLI not found on PATH")

    cmd = [binary, "-p", request.prompt, "--output-format", "json", "--model", request.model]

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

    return replace(_parse_envelope(raw), model=request.model)
