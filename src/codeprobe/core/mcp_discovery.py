"""Shared MCP configuration discovery logic.

Extracted from ``cli/init_cmd.py`` so that multiple commands (init, experiment
add-config, mine) can locate MCP server configurations without duplication.
"""

from __future__ import annotations

import json
from pathlib import Path

# Known locations for MCP config files, searched in order.
MCP_SEARCH_PATHS: list[Path] = [
    Path.home() / ".claude" / ".mcp.json",
    Path.home() / ".claude" / "mcp-configs" / "mcp-servers.json",
    Path.home() / ".claude" / "settings.json",
    Path.home() / ".claude" / "settings.local.json",
]


def discover_mcp_configs() -> list[tuple[Path, list[str]]]:
    """Scan known locations for MCP config files with mcpServers keys.

    Returns a list of ``(path, server_names)`` for each file that has servers.
    """
    found: list[tuple[Path, list[str]]] = []
    for p in MCP_SEARCH_PATHS:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        servers = data.get("mcpServers", {})
        if servers:
            found.append((p, sorted(servers.keys())))
    # Also check .mcp.json in the current directory
    local_mcp = Path.cwd() / ".mcp.json"
    if local_mcp.is_file():
        try:
            data = json.loads(local_mcp.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {})
            if servers:
                found.append((local_mcp, sorted(servers.keys())))
        except (json.JSONDecodeError, OSError):
            pass
    return found
