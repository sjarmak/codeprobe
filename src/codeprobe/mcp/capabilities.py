"""Versioned MCP capability registry.

A *capability* describes an abstract operation an MCP server can perform
(e.g. keyword search, symbol reference lookup). Concrete MCP tools provided
by a given server implement one or more capabilities. The capability layer
intentionally avoids referencing any concrete tool name so that preambles,
test fixtures, and evaluations can be written against capabilities and then
bound to whatever tools a server happens to expose.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

CAPABILITIES_VERSION = "1.0.0"

# Capability identifiers — the public constants other modules should import
# rather than string-literal-typing these names.
KEYWORD_SEARCH = "KEYWORD_SEARCH"
SYMBOL_REFERENCES = "SYMBOL_REFERENCES"
FILE_READ = "FILE_READ"
GO_TO_DEFINITION = "GO_TO_DEFINITION"


@dataclass(frozen=True)
class Capability:
    """An abstract MCP capability.

    Attributes:
        id: Stable identifier (uppercase snake case, e.g. ``KEYWORD_SEARCH``).
        name: Human/machine readable short name (lowercase snake case).
        description: Plain-text description of the operation.
        input_schema: JSON-schema-like mapping describing expected inputs.
    """

    id: str
    name: str
    description: str
    input_schema: Mapping[str, Any] = field(default_factory=dict)


def _freeze_schema(schema: dict[str, Any]) -> Mapping[str, Any]:
    """Return a read-only view of the given schema dict."""
    return MappingProxyType(dict(schema))


_CAPABILITY_LIST: tuple[Capability, ...] = (
    Capability(
        id=KEYWORD_SEARCH,
        name="keyword_search",
        description=(
            "Search the indexed codebase for occurrences of one or more keywords "
            "and return ranked matches with file path and line number."
        ),
        input_schema=_freeze_schema(
            {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Keyword or phrase to search for."},
                    "limit": {"type": "integer", "minimum": 1, "default": 20},
                },
                "required": ["query"],
            }
        ),
    ),
    Capability(
        id=SYMBOL_REFERENCES,
        name="symbol_references",
        description=(
            "Return all call sites and references to a named symbol across the "
            "codebase, grouped by file."
        ),
        input_schema=_freeze_schema(
            {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Fully qualified symbol name."},
                    "include_declarations": {"type": "boolean", "default": False},
                },
                "required": ["symbol"],
            }
        ),
    ),
    Capability(
        id=FILE_READ,
        name="file_read",
        description=(
            "Read the contents of a file from the indexed workspace. Supports "
            "optional line-range slicing for large files."
        ),
        input_schema=_freeze_schema(
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative path."},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
            }
        ),
    ),
    Capability(
        id=GO_TO_DEFINITION,
        name="go_to_definition",
        description=(
            "Resolve a symbol at a given file/line location to its definition "
            "site, returning file path and line number."
        ),
        input_schema=_freeze_schema(
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "line": {"type": "integer", "minimum": 1},
                    "column": {"type": "integer", "minimum": 1},
                },
                "required": ["path", "line"],
            }
        ),
    ),
)


CAPABILITIES: Mapping[str, Capability] = MappingProxyType(
    {cap.id: cap for cap in _CAPABILITY_LIST}
)


def get_capability(capability_id: str) -> Capability:
    """Look up a capability by its identifier.

    Args:
        capability_id: One of the module-level constants (e.g. ``KEYWORD_SEARCH``).

    Raises:
        KeyError: If no capability with the given id is registered.
    """
    try:
        return CAPABILITIES[capability_id]
    except KeyError as exc:
        raise KeyError(f"Unknown MCP capability id: {capability_id!r}") from exc


def list_capabilities() -> tuple[Capability, ...]:
    """Return all registered capabilities in a deterministic order."""
    return _CAPABILITY_LIST


__all__ = [
    "CAPABILITIES",
    "CAPABILITIES_VERSION",
    "Capability",
    "FILE_READ",
    "GO_TO_DEFINITION",
    "KEYWORD_SEARCH",
    "SYMBOL_REFERENCES",
    "get_capability",
    "list_capabilities",
]
