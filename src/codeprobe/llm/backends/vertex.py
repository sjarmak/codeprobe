"""Backend shim for Google Vertex AI (Anthropic models via GCP).

Lazily imports ``anthropic.AnthropicVertex``. GCP credentials are
resolved via the standard ``GOOGLE_APPLICATION_CREDENTIALS`` /
``gcloud`` auth chain; we check for the project/region env vars to
surface a clean unavailable error rather than letting the SDK raise
a deep GCP error.
"""

from __future__ import annotations

import os
from typing import Any

from codeprobe.llm import ModelRegistry, get_registry
from codeprobe.llm.backends.base import (
    BackendExecutionError,
    BackendUnavailableError,
)

__all__ = ["VertexBackend"]


class VertexBackend:
    """Thin adapter over ``anthropic.AnthropicVertex``."""

    name: str = "vertex"

    PROJECT_ENV: str = "GOOGLE_CLOUD_PROJECT"
    REGION_ENV: str = "GOOGLE_CLOUD_REGION"

    def __init__(self, registry: ModelRegistry | None = None) -> None:
        self._registry = registry or get_registry()

    def resolve_model_id(self, logical_name: str) -> str:
        value = self._registry.resolve(logical_name, self.name)
        if not isinstance(value, str) or not value:
            raise BackendExecutionError(
                f"Registry entry for vertex/{logical_name} must be a "
                f"non-empty publisher path, got {type(value).__name__}"
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
                "VertexBackend.complete requires a string model_id"
            )

        try:
            from anthropic import AnthropicVertex
        except ImportError as exc:
            raise BackendUnavailableError(
                "anthropic[vertex] not installed — required for Vertex backend"
            ) from exc

        project = os.environ.get(self.PROJECT_ENV)
        region = os.environ.get(self.REGION_ENV)
        if not project or not region:
            raise BackendUnavailableError(
                f"{self.PROJECT_ENV} and {self.REGION_ENV} must both be set "
                "for the Vertex backend"
            )

        # The Vertex SDK accepts the bare model slug (last path segment),
        # so strip "publishers/.../models/" if present.
        vertex_model = model_id.split("/")[-1] if "/" in model_id else model_id

        client = AnthropicVertex(project_id=project, region=region)
        max_tokens = int(kwargs.pop("max_tokens", 1024))
        try:
            message = client.messages.create(
                model=vertex_model,
                max_tokens=max_tokens,
                messages=messages,
                **kwargs,
            )
        except Exception as exc:  # pragma: no cover - network path
            raise BackendExecutionError(f"Vertex API error: {exc}") from exc

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
