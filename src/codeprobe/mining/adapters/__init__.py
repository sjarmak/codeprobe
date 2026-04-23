"""Narrative adapters — pluggable sources for per-commit enrichment context.

Each adapter implements :class:`codeprobe.mining.sources.NarrativeAdapter`.
They are intentionally small and side-effect-light: mechanical subprocess
calls + file IO, no semantic judgment (ZFC compliant).
"""

from __future__ import annotations

from codeprobe.mining.adapters.commit import CommitAdapter
from codeprobe.mining.adapters.pr import PRAdapter
from codeprobe.mining.adapters.rfc import RFCAdapter
from codeprobe.mining.sources import NarrativeAdapter

__all__ = [
    "CommitAdapter",
    "PRAdapter",
    "RFCAdapter",
    "build_adapter",
]


def build_adapter(name: str) -> NarrativeAdapter:
    """Construct the adapter registered under *name*.

    Raises :class:`KeyError` for unknown names. Callers in the mining
    pipeline should validate names up front via
    :func:`codeprobe.mining.sources.select_narrative_adapters`.
    """
    if name == "pr":
        return PRAdapter()
    if name == "commits":
        return CommitAdapter()
    if name == "rfcs":
        return RFCAdapter()
    raise KeyError(f"No narrative adapter registered for {name!r}")
