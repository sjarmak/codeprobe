"""Per-vendor LLM backend adapters implementing :class:`LLMBackend`.

Each backend is a thin shim over a vendor SDK. SDK imports are deferred
until :meth:`complete` is called so that importing a backend module does
not require the optional dependency to be installed. This keeps
``pytest`` collection green in environments that only have a subset of
vendor SDKs available (e.g. CI without AWS or GCP credentials).
"""

from __future__ import annotations

from codeprobe.llm.backends.anthropic import AnthropicBackend
from codeprobe.llm.backends.azure_openai import AzureOpenAIBackend
from codeprobe.llm.backends.base import (
    BackendError,
    BackendExecutionError,
    BackendUnavailableError,
    LLMBackend,
)
from codeprobe.llm.backends.bedrock import BedrockBackend
from codeprobe.llm.backends.openai_compat import OpenAICompatBackend
from codeprobe.llm.backends.vertex import VertexBackend

__all__ = [
    "AnthropicBackend",
    "AzureOpenAIBackend",
    "BackendError",
    "BackendExecutionError",
    "BackendUnavailableError",
    "BedrockBackend",
    "LLMBackend",
    "OpenAICompatBackend",
    "VertexBackend",
    "get_backend",
    "BACKEND_CLASSES",
]


BACKEND_CLASSES: dict[str, type[LLMBackend]] = {
    "anthropic": AnthropicBackend,
    "bedrock": BedrockBackend,
    "vertex": VertexBackend,
    "azure_openai": AzureOpenAIBackend,
    "openai_compat": OpenAICompatBackend,
}


def get_backend(name: str) -> LLMBackend:
    """Instantiate a backend by its registry name."""
    try:
        cls = BACKEND_CLASSES[name]
    except KeyError as exc:
        known = ", ".join(sorted(BACKEND_CLASSES.keys()))
        raise BackendError(
            f"Unknown backend {name!r}. Known: {known}"
        ) from exc
    return cls()
