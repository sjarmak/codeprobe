"""Backend shim for generic OpenAI-compatible gateways.

Works against any service that speaks the OpenAI Chat Completions API
(vLLM, LiteLLM, Together, Groq, Ollama via ``/v1``, etc.). The base
URL is provided via ``OPENAI_COMPAT_BASE_URL`` and the API key via
``OPENAI_COMPAT_API_KEY`` (falls back to ``OPENAI_API_KEY`` so users
who have already wired up the OpenAI SDK can reuse the env var).
"""

from __future__ import annotations

import os
from typing import Any

from codeprobe.llm import ModelRegistry, get_registry
from codeprobe.llm.backends.base import (
    BackendExecutionError,
    BackendUnavailableError,
)

__all__ = ["OpenAICompatBackend"]


class OpenAICompatBackend:
    """Thin adapter over ``openai.OpenAI`` with a configurable ``base_url``."""

    name: str = "openai_compat"

    BASE_URL_ENV: str = "OPENAI_COMPAT_BASE_URL"
    API_KEY_ENVS: tuple[str, ...] = ("OPENAI_COMPAT_API_KEY", "OPENAI_API_KEY")

    def __init__(self, registry: ModelRegistry | None = None) -> None:
        self._registry = registry or get_registry()

    def resolve_model_id(self, logical_name: str) -> str:
        value = self._registry.resolve(logical_name, self.name)
        if not isinstance(value, str) or not value:
            raise BackendExecutionError(
                f"Registry entry for openai_compat/{logical_name} must be a "
                f"non-empty string, got {type(value).__name__}"
            )
        return value

    def _resolve_api_key(self) -> str | None:
        for key in self.API_KEY_ENVS:
            value = os.environ.get(key)
            if value:
                return value
        return None

    def complete(
        self,
        messages: list[dict[str, Any]],
        model_id: str | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not isinstance(model_id, str):
            raise BackendExecutionError(
                "OpenAICompatBackend.complete requires a string model_id"
            )

        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - dep is required
            raise BackendUnavailableError(
                "openai SDK not installed (pip install openai>=1.0)"
            ) from exc

        base_url = os.environ.get(self.BASE_URL_ENV)
        api_key = self._resolve_api_key()
        if not base_url:
            raise BackendUnavailableError(
                f"{self.BASE_URL_ENV} must be set for the openai_compat backend"
            )
        if not api_key:
            raise BackendUnavailableError(
                "No API key: set one of "
                + ", ".join(self.API_KEY_ENVS)
            )

        client = OpenAI(api_key=api_key, base_url=base_url)
        max_tokens = int(kwargs.pop("max_tokens", 1024))
        try:
            response = client.chat.completions.create(
                model=model_id,
                messages=messages,  # type: ignore[arg-type]
                max_tokens=max_tokens,
                **kwargs,
            )
        except Exception as exc:  # pragma: no cover - network path
            raise BackendExecutionError(
                f"OpenAI-compatible gateway error: {exc}"
            ) from exc

        choice = response.choices[0] if response.choices else None
        text = ""
        if choice and choice.message and choice.message.content:
            text = choice.message.content

        usage = response.usage
        return {
            "text": text,
            "model": model_id,
            "backend": self.name,
            "usage": {
                "input_tokens": getattr(usage, "prompt_tokens", None),
                "output_tokens": getattr(usage, "completion_tokens", None),
            },
        }
