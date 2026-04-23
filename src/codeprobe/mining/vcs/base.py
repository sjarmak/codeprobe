"""Shared Protocols, dataclasses, and redaction primitives for VCS adapters.

Every adapter that talks to a hosted VCS / issue tracker MUST:

1. Implement ``redact_request(req)`` / ``redact_response(resp)`` that route through
   :func:`redact` using the adapter's ``_known_tokens`` set.
2. Route every log call through ``self._log(...)`` so messages pass through the
   redactor BEFORE they reach any handler.
3. Raise :class:`AuthFailure` on HTTP 401/403 — never silently degrade.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "AuthFailure",
    "AuthMode",
    "MergeRequest",
    "VCSAdapter",
    "redact",
]


class AuthMode(str, Enum):
    """Authentication mode used by VCS / tracker adapters."""

    PAT = "pat"
    OAUTH2 = "oauth2"


class AuthFailure(RuntimeError):
    """Raised when an adapter receives HTTP 401/403.

    The exception message always includes a concrete remediation URL so the
    user knows exactly where to rotate the failing credential.
    """

    def __init__(self, adapter: str, status: int, remediation_url: str) -> None:
        super().__init__(
            f"{adapter} auth failure (HTTP {status}). "
            f"Rotate or recreate your token at: {remediation_url}"
        )
        self.adapter = adapter
        self.status = status
        self.remediation_url = remediation_url


@dataclass(frozen=True)
class MergeRequest:
    """Minimal shape for a merge / pull request used across VCS adapters."""

    id: int
    iid: int
    title: str
    description: str
    state: str
    source_branch: str
    target_branch: str
    merge_commit_sha: str
    web_url: str
    author: str = ""
    labels: tuple[str, ...] = ()


@runtime_checkable
class VCSAdapter(Protocol):
    """Protocol every VCS adapter must satisfy.

    Implementations MUST apply ``redact_request`` / ``redact_response`` to
    anything they log or emit as an event.
    """

    name: str

    def list_merges(
        self, project: str, *, limit: int = 20
    ) -> Iterator[MergeRequest]:  # pragma: no cover - Protocol stub
        ...

    def pr_context(
        self, project: str, mr_iid: int
    ) -> dict[str, Any]:  # pragma: no cover - Protocol stub
        ...

    def redact_request(
        self, req: dict[str, Any]
    ) -> dict[str, Any]:  # pragma: no cover - Protocol stub
        ...

    def redact_response(
        self, resp: dict[str, Any]
    ) -> dict[str, Any]:  # pragma: no cover - Protocol stub
        ...


_REDACTED = "[REDACTED-TOKEN]"


def redact(obj: Any, known_tokens: set[str]) -> Any:
    """Return a structural copy of *obj* with every occurrence of a known
    token replaced by ``[REDACTED-TOKEN]``.

    Walks ``str`` / ``dict`` / ``list`` / ``tuple``. Other types are returned
    unchanged — by design we never serialize unknown objects into logs.

    Mechanical replacement only. No regex heuristics, no semantic judgment.
    """
    if not known_tokens:
        return obj
    non_empty = {t for t in known_tokens if t}
    if not non_empty:
        return obj
    if isinstance(obj, str):
        out = obj
        # Replace longer tokens first so a short token that happens to be a
        # substring of a longer one doesn't short-circuit the longer match.
        for tok in sorted(non_empty, key=len, reverse=True):
            if tok in out:
                out = out.replace(tok, _REDACTED)
        return out
    if isinstance(obj, dict):
        return {
            redact(k, non_empty): redact(v, non_empty) for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(x, non_empty) for x in obj]
    if isinstance(obj, tuple):
        return tuple(redact(x, non_empty) for x in obj)
    return obj


class RedactingLoggerMixin:
    """Mixin providing a ``_log`` helper that redacts before emitting.

    Concrete adapters must assign ``self._known_tokens: set[str]`` and
    ``self._logger: logging.Logger`` in ``__init__``.
    """

    _known_tokens: set[str]
    _logger: logging.Logger

    def _log(
        self, level: int, msg: str, *args: Any, **extra: Any
    ) -> None:
        """Log ``msg`` after applying token redaction.

        ``args`` are redacted individually; ``extra`` (keyword payload) is
        redacted as a dict and passed as ``extra={...}`` to the logger so it
        survives through formatters.
        """
        safe_msg = redact(msg, self._known_tokens)
        safe_args = tuple(redact(a, self._known_tokens) for a in args)
        safe_extra = redact(extra, self._known_tokens) if extra else None
        if safe_extra:
            self._logger.log(level, safe_msg, *safe_args, extra={"payload": safe_extra})
        else:
            self._logger.log(level, safe_msg, *safe_args)
