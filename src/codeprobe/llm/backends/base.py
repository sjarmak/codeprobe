"""Shared protocol and errors for LLM backends."""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "BackendError",
    "BackendExecutionError",
    "BackendUnavailableError",
    "LLMBackend",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class BackendError(Exception):
    """Base class for backend-level errors."""


class BackendUnavailableError(BackendError):
    """Raised when a backend cannot be used (SDK missing, creds absent)."""


class BackendExecutionError(BackendError):
    """Raised when a backend call fails at the SDK/API level."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMBackend(Protocol):
    """Minimal contract for a logical -> vendor LLM adapter.

    Backends are thin shims: they delegate logical-name resolution to the
    shared registry and route chat-completion calls to a vendor SDK.

    Attributes:
        name: Registry key for this backend (e.g. ``"anthropic"``).
    """

    name: str

    def resolve_model_id(
        self, logical_name: str
    ) -> str | dict[str, Any]:
        """Return the per-backend identifier for a logical model name.

        Implementations typically delegate to the shared
        :class:`codeprobe.llm.ModelRegistry`.
        """
        ...

    def complete(
        self,
        messages: list[dict[str, Any]],
        model_id: str | dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Execute a chat-completion request.

        Args:
            messages: OpenAI-style ``[{"role": ..., "content": ...}, ...]``.
            model_id: The identifier returned by
                :meth:`resolve_model_id` (str for most backends, dict for
                Azure OpenAI where deployment + api_version are required).
            **kwargs: Optional per-call params (``max_tokens``,
                ``temperature``, etc.) forwarded to the vendor SDK.

        Returns:
            Dict with at minimum ``{"text": str, "model": str,
            "backend": str, "usage": {...}}``.

        Raises:
            BackendUnavailableError: SDK missing or credentials absent.
            BackendExecutionError: Vendor API returned an error.
        """
        ...
