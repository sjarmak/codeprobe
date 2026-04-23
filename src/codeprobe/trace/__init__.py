"""SQLite-backed agent trace store.

Captures agent lifecycle events (tool calls, results) to
``runs/trace.db`` with write-time budget enforcement (INV1) and
content-policy redaction applied before events enter the store.
"""

from __future__ import annotations

from codeprobe.trace.content_policy import ContentPolicy
from codeprobe.trace.recorder import (
    TraceBudgetExceeded,
    TraceOverflowPolicy,
    TraceRecorder,
)
from codeprobe.trace.store import SCHEMA_VERSION, export_jsonl, open_store

__all__ = [
    "ContentPolicy",
    "SCHEMA_VERSION",
    "TraceBudgetExceeded",
    "TraceOverflowPolicy",
    "TraceRecorder",
    "export_jsonl",
    "open_store",
]
