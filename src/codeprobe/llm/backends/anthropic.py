"""Backend shim for Anthropic's public API.

Lazily imports the ``anthropic`` SDK so that this module can be imported
in environments that lack the dependency (although ``anthropic`` is a
required top-level dep of codeprobe today).
"""

from __future__ import annotations

import os
from typing import Any

from codeprobe.llm import ModelRegistry, get_registry
from codeprobe.llm.backends.base import (
    BackendExecutionError,
    BackendUnavailableError,
)

__all__ = ["AnthropicBackend"]


class AnthropicBackend:
    """Thin adapter over ``anthropic.Anthropic``."""

    name: str = "anthropic"

    def __init__(self, registry: ModelRegistry | None = None) -> None:
        self._registry = registry or get_registry()

    def resolve_model_id(self, logical_name: str) -> str:
        value = self._registry.resolve(logical_name, self.name)
        if not isinstance(value, str) or not value:
            raise BackendExecutionError(
                f"Registry entry for anthropic/{logical_name} must be a "
                f"non-empty string, got {type(value).__name__}"
            )
        return value

    def complete(
        self,
        messages: list[dict[str, Any]],
        model_id: str | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not isinstance(model_id, str):
            raise BackendExecutionError(
                "AnthropicBackend.complete requires a string model_id"
            )

        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - dep is required
            raise BackendUnavailableError(
                "anthropic SDK not installed (pip install anthropic)"
            ) from exc

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise BackendUnavailableError("ANTHROPIC_API_KEY not set")

        client = anthropic.Anthropic(api_key=api_key)
        max_tokens = int(kwargs.pop("max_tokens", 1024))
        try:
            message = client.messages.create(
                model=model_id,
                max_tokens=max_tokens,
                messages=messages,
                **kwargs,
            )
        except Exception as exc:  # pragma: no cover - network path
            raise BackendExecutionError(f"Anthropic API error: {exc}") from exc

        text = ""
        for block in message.content:
            if hasattr(block, "text"):
                text += block.text

        return {
            "text": text,
            "model": model_id,
            "backend": self.name,
            "usage": {
                "input_tokens": getattr(message.usage, "input_tokens", None),
                "output_tokens": getattr(message.usage, "output_tokens", None),
            },
        }
