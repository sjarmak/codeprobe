"""MCP (Model Context Protocol) capability layer for codeprobe.

Exposes a versioned registry of capabilities that MCP servers may provide.
Capabilities are named by semantic role (e.g. ``KEYWORD_SEARCH``), never by
concrete tool name — this keeps the capability layer portable across MCP
server implementations.
"""

from __future__ import annotations

from codeprobe.mcp.capabilities import (
    CAPABILITIES,
    CAPABILITIES_VERSION,
    Capability,
    get_capability,
    list_capabilities,
)

__all__ = [
    "CAPABILITIES",
    "CAPABILITIES_VERSION",
    "Capability",
    "get_capability",
    "list_capabilities",
]
