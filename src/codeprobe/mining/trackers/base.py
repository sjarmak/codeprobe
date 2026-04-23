"""Shared Protocol + dataclass for issue tracker adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

__all__ = ["IssueAdapter", "Ticket"]


@dataclass(frozen=True)
class Ticket:
    """Minimal shape for a tracker ticket."""

    key: str
    title: str
    body: str
    status: str


@runtime_checkable
class IssueAdapter(Protocol):
    """Protocol every issue tracker adapter must satisfy."""

    name: str

    def fetch_ticket(self, ref: str) -> Ticket:  # pragma: no cover - Protocol stub
        ...

    def redact_request(
        self, req: dict[str, Any]
    ) -> dict[str, Any]:  # pragma: no cover - Protocol stub
        ...

    def redact_response(
        self, resp: dict[str, Any]
    ) -> dict[str, Any]:  # pragma: no cover - Protocol stub
        ...
