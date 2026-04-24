"""SQLite-backed agent trace store.

Captures agent lifecycle events (tool calls, results) to
``runs/trace.db`` with write-time budget enforcement (INV1) and
content-policy redaction applied before events enter the store.
"""

from __future__ import annotations

from codeprobe.trace.content_policy import ContentPolicy
from codeprobe.trace.recorder import (
    TraceBudgetExceededError,
    TraceOverflowPolicy,
    TraceRecorder,
)
from codeprobe.trace.store import SCHEMA_VERSION, export_jsonl, open_store


def __getattr__(name: str) -> object:
    """Re-export shim for ``TraceBudgetExceeded`` → ``TraceBudgetExceededError`` (N818)."""
    if name == "TraceBudgetExceeded":
        from codeprobe.trace.recorder import __getattr__ as _rec_getattr

        return _rec_getattr(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ContentPolicy",
    "SCHEMA_VERSION",
    "TraceBudgetExceededError",
    "TraceOverflowPolicy",
    "TraceRecorder",
    "export_jsonl",
    "open_store",
]
