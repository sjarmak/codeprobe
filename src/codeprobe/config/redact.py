"""Redact secrets from MCP config dicts before logging or serialization.

All Authorization header values are unconditionally replaced with
``[REDACTED]`` to prevent accidental exposure in logs, experiment.json,
or repr() output.
"""

from __future__ import annotations

import copy

_SENSITIVE_HEADER_NAMES = frozenset({"authorization"})


def redact_mcp_headers(mcp_config: dict | None) -> dict | None:
    """Return a deep copy of *mcp_config* with Authorization values redacted.

    Returns ``None`` when *mcp_config* is ``None``.
    Returns an empty dict when *mcp_config* is empty.
    Non-standard structures (no ``mcpServers`` key) pass through unchanged.
    The original dict is never mutated.
    """
    if mcp_config is None:
        return None
    if not mcp_config:
        return {}

    result = copy.deepcopy(mcp_config)

    servers = result.get("mcpServers")
    if not isinstance(servers, dict):
        return result

    for _name, server_cfg in servers.items():
        if not isinstance(server_cfg, dict):
            continue
        headers = server_cfg.get("headers")
        if not isinstance(headers, dict):
            continue
        for key in list(headers):
            if key.lower() in _SENSITIVE_HEADER_NAMES:
                if isinstance(headers[key], str):
                    headers[key] = "[REDACTED]"

    return result
