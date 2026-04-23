"""Backend shim for Azure-hosted OpenAI models.

Azure OpenAI requires three pieces of config: a deployment name (per
model instance), an ``api_version``, and an account-level endpoint.
The registry stores ``{deployment, api_version}`` per logical model;
the endpoint is read from ``AZURE_OPENAI_ENDPOINT`` at call time.
"""

from __future__ import annotations

import os
from typing import Any

from codeprobe.llm import ModelRegistry, get_registry
from codeprobe.llm.backends.base import (
    BackendExecutionError,
    BackendUnavailableError,
)

__all__ = ["AzureOpenAIBackend"]


class AzureOpenAIBackend:
    """Thin adapter over ``openai.AzureOpenAI``."""

    name: str = "azure_openai"

    ENDPOINT_ENV: str = "AZURE_OPENAI_ENDPOINT"
    API_KEY_ENV: str = "AZURE_OPENAI_API_KEY"

    def __init__(self, registry: ModelRegistry | None = None) -> None:
        self._registry = registry or get_registry()

    def resolve_model_id(self, logical_name: str) -> dict[str, Any]:
        value = self._registry.resolve(logical_name, self.name)
        if not isinstance(value, dict):
            raise BackendExecutionError(
                f"Registry entry for azure_openai/{logical_name} must be a "
                f"dict with 'deployment' and 'api_version', got "
                f"{type(value).__name__}"
            )
        for key in ("deployment", "api_version"):
            if not value.get(key):
                raise BackendExecutionError(
                    f"azure_openai/{logical_name} missing required key "
                    f"{key!r} in registry"
                )
        return dict(value)

    def complete(
        self,
        messages: list[dict[str, Any]],
        model_id: str | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if not isinstance(model_id, dict):
            raise BackendExecutionError(
                "AzureOpenAIBackend.complete requires a dict model_id "
                "with 'deployment' and 'api_version'"
            )

        try:
            from openai import AzureOpenAI
        except ImportError as exc:  # pragma: no cover - dep is required
            raise BackendUnavailableError(
                "openai SDK not installed (pip install openai>=1.0)"
            ) from exc

        endpoint = os.environ.get(self.ENDPOINT_ENV)
        api_key = os.environ.get(self.API_KEY_ENV)
        if not endpoint or not api_key:
            raise BackendUnavailableError(
                f"{self.ENDPOINT_ENV} and {self.API_KEY_ENV} must both be set"
            )

        deployment = str(model_id["deployment"])
        api_version = str(model_id["api_version"])

        client = AzureOpenAI(
            api_key=api_key,
            api_version=api_version,
            azure_endpoint=endpoint,
        )
        max_tokens = int(kwargs.pop("max_tokens", 1024))
        try:
            response = client.chat.completions.create(
                model=deployment,
                messages=messages,
                max_tokens=max_tokens,
                **kwargs,
            )
        except Exception as exc:  # pragma: no cover - network path
            raise BackendExecutionError(f"Azure OpenAI API error: {exc}") from exc

        choice = response.choices[0] if response.choices else None
        text = ""
        if choice and choice.message and choice.message.content:
            text = choice.message.content

        usage = response.usage
        return {
            "text": text,
            "model": deployment,
            "backend": self.name,
            "usage": {
                "input_tokens": getattr(usage, "prompt_tokens", None),
                "output_tokens": getattr(usage, "completion_tokens", None),
            },
        }
