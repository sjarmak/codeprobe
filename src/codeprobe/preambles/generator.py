"""Preamble rendering from a capability set and user-supplied tool table.

The "custom" preamble is a user-facing generic template: given a list of
capability IDs (KEYWORD_SEARCH, SYMBOL_REFERENCES, …) and a tool table
(``[{name, description, capability_id}, ...]``), render a Markdown preamble
that describes the tools the agent should use — without hardcoding any
vendor-specific tool names in codeprobe itself.

ZFC note: this function is pure, mechanical template substitution. No
heuristic scoring or semantic judgment — the tool descriptions come
verbatim from the caller.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from codeprobe.mcp.capabilities import CAPABILITIES, Capability

_TEMPLATE_DIR = Path(__file__).resolve().parent
_TEMPLATE_NAME = "custom.md.j2"

_REQUIRED_TOOL_KEYS = frozenset({"name", "description", "capability_id"})


def _env() -> Environment:
    """Build a Jinja2 environment rooted at the preambles directory.

    ``StrictUndefined`` ensures a missing tool field surfaces as a clear
    template error rather than silently rendering an empty string.
    """
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=False,  # Markdown output, not HTML
        undefined=StrictUndefined,
        keep_trailing_newline=True,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def _resolve_capabilities(ids: Sequence[str]) -> list[Capability]:
    """Resolve capability IDs against the live registry.

    Preserves the caller's ordering.

    Raises:
        KeyError: if any ID is not present in CAPABILITIES.
    """
    resolved: list[Capability] = []
    for cap_id in ids:
        if cap_id not in CAPABILITIES:
            raise KeyError(
                f"Unknown capability id: {cap_id!r}. "
                f"Known: {sorted(CAPABILITIES.keys())}"
            )
        resolved.append(CAPABILITIES[cap_id])
    return resolved


def _validate_tool_table(
    tool_table: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    """Validate the tool table shape.

    Each entry must be a mapping with exactly the required keys. Unknown
    ``capability_id`` values pass through verbatim — callers are responsible
    for keeping them consistent with the registry when that matters.

    Returns a list of plain dicts ready for Jinja rendering.
    """
    validated: list[dict[str, str]] = []
    for i, entry in enumerate(tool_table):
        if not isinstance(entry, Mapping):
            raise TypeError(
                f"tool_table[{i}] must be a mapping, got {type(entry).__name__}"
            )
        missing = _REQUIRED_TOOL_KEYS - entry.keys()
        if missing:
            raise ValueError(
                f"tool_table[{i}] missing required keys: {sorted(missing)}; "
                f"each entry needs {sorted(_REQUIRED_TOOL_KEYS)}"
            )
        name = entry["name"]
        description = entry["description"]
        capability_id = entry["capability_id"]
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"tool_table[{i}].name must be a non-empty string")
        if not isinstance(description, str):
            raise ValueError(f"tool_table[{i}].description must be a string")
        if not isinstance(capability_id, str) or not capability_id.strip():
            raise ValueError(
                f"tool_table[{i}].capability_id must be a non-empty string"
            )
        validated.append(
            {
                "name": name,
                "description": description,
                "capability_id": capability_id,
            }
        )
    return validated


def render_preamble(
    capability_set: Sequence[str],
    tool_table: Sequence[Mapping[str, str]],
) -> str:
    """Render the custom preamble.

    Args:
        capability_set: capability IDs (must exist in
            :data:`codeprobe.mcp.capabilities.CAPABILITIES`) to include in the
            "Capabilities" section of the rendered preamble. Caller ordering is
            preserved in the output.
        tool_table: list of ``{name, description, capability_id}`` dicts. Each
            tool's ``capability_id`` is written verbatim into the rendered
            Markdown.

    Returns:
        Rendered Markdown string.

    Raises:
        KeyError:   if a capability ID in ``capability_set`` is not registered.
        ValueError: if the tool table is malformed.
        TypeError:  if ``tool_table`` entries are not mappings.
    """
    if not capability_set:
        raise ValueError("capability_set must be non-empty")
    if not tool_table:
        raise ValueError("tool_table must be non-empty")

    capabilities = _resolve_capabilities(capability_set)
    tools = _validate_tool_table(tool_table)

    env = _env()
    template = env.get_template(_TEMPLATE_NAME)
    rendered: Any = template.render(capabilities=capabilities, tools=tools)
    return str(rendered)


__all__ = ["render_preamble"]
