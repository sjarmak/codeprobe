"""Jinja-based preamble template registry.

Renders preamble bodies from MCP capability descriptions. Templates live in
this package as ``*.md.j2`` files and are loaded via Jinja2's ``FileSystemLoader``.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

from codeprobe.mcp.capabilities import Capability, list_capabilities

_TEMPLATES_DIR = Path(__file__).resolve().parent


def _build_environment() -> Environment:
    """Create a Jinja2 environment rooted at the templates directory."""
    return Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(default=False),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
    )


def list_templates() -> list[str]:
    """Return the names of all available preamble templates."""
    return sorted(p.name for p in _TEMPLATES_DIR.glob("*.j2"))


def render(
    template_name: str,
    *,
    capabilities: Iterable[Capability] | None = None,
    **extra_context: Any,
) -> str:
    """Render a preamble template.

    Args:
        template_name: File name of the template within this package
            (e.g. ``mcp_base.md.j2``).
        capabilities: Iterable of :class:`Capability` instances. If ``None``,
            the full registry from :func:`list_capabilities` is used.
        **extra_context: Additional values passed to the template.

    Returns:
        Rendered preamble body as a string.
    """
    env = _build_environment()
    template = env.get_template(template_name)
    caps: tuple[Capability, ...] = (
        tuple(capabilities) if capabilities is not None else list_capabilities()
    )
    return template.render(capabilities=caps, **extra_context)


__all__ = ["list_templates", "render"]
