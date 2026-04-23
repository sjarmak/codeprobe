"""In-process MCP fixture server.

Exposes a deterministic ``tools/list`` response derived from the capability
registry. Used in tests and offline replay scenarios where a real MCP server
is unavailable.

This module purposefully does not speak the JSON-RPC wire format — it exposes
Python functions returning the response *body* so tests can assert on shape
directly. A thin HTTP/stdio shim can be layered on top later without changing
this interface.
"""

from __future__ import annotations

from typing import Any

from codeprobe.mcp.capabilities import Capability, list_capabilities

# Mapping of capability id -> concrete tool name used by the fixture.
# The fixture is free to pick tool names; the capability layer itself
# remains tool-name agnostic.
_FIXTURE_TOOL_NAMES: dict[str, str] = {
    "KEYWORD_SEARCH": "fixture_keyword_search",
    "SYMBOL_REFERENCES": "fixture_symbol_references",
    "FILE_READ": "fixture_file_read",
    "GO_TO_DEFINITION": "fixture_go_to_definition",
}


def _tool_from_capability(cap: Capability) -> dict[str, Any]:
    """Return an MCP tool schema dict for a capability."""
    tool_name = _FIXTURE_TOOL_NAMES.get(cap.id, f"fixture_{cap.name}")
    return {
        "name": tool_name,
        "description": cap.description,
        "inputSchema": dict(cap.input_schema),
    }


def list_tools() -> list[dict[str, Any]]:
    """Return the deterministic list of tool schemas.

    Each entry mirrors the MCP ``Tool`` shape: ``{name, description,
    inputSchema}``.
    """
    return [_tool_from_capability(cap) for cap in list_capabilities()]


def tools_list_response() -> dict[str, Any]:
    """Return the full MCP ``tools/list`` response body.

    Shape: ``{"tools": [ ... ]}`` — matching the MCP protocol response payload.
    """
    return {"tools": list_tools()}


__all__ = ["list_tools", "tools_list_response"]
