"""Redact secrets from MCP config dicts before logging or serialization.

All Authorization header values are unconditionally replaced with
``[REDACTED]`` to prevent accidental exposure in logs, experiment.json,
or repr() output.

CLI-arg patterns (``--header "Authorization: token sgp_..."``), env values
containing known token prefixes, and other secret-shaped strings are also
redacted.
"""

from __future__ import annotations

import copy
import re

_SENSITIVE_HEADER_NAMES = frozenset({"authorization"})

# Prefixes that indicate a secret token value.  Kept intentionally broad —
# false positives (redacting a non-secret that starts with ``sk-``) are
# strictly better than false negatives (leaking a real key).
_TOKEN_PREFIXES = (
    "sgp_",  # Sourcegraph
    "ghp_",  # GitHub PAT
    "gho_",  # GitHub OAuth
    "ghs_",  # GitHub App
    "ghr_",  # GitHub Refresh
    "glpat-",  # GitLab PAT
    "sk-",  # OpenAI / Anthropic
    "sk-proj-",  # OpenAI project-scoped
    "sk-ant-",  # Anthropic
    "xoxb-",  # Slack bot
    "xoxp-",  # Slack user
    "xoxa-",  # Slack app
)

_AUTH_HEADER_RE = re.compile(
    r"^(Authorization:\s*(?:token|Bearer)\s+)\S+",
    re.IGNORECASE,
)


def _is_secret(value: str) -> bool:
    """Heuristic: does *value* look like a secret token?"""
    return any(value.startswith(prefix) for prefix in _TOKEN_PREFIXES)


def _redact_auth_arg(value: str) -> str:
    """Redact the token portion of an ``Authorization: <scheme> <token>`` string."""
    m = _AUTH_HEADER_RE.match(value)
    if m:
        return m.group(1) + "[REDACTED]"
    return value


def redact_mcp_headers(mcp_config: dict | None) -> dict | None:
    """Return a deep copy of *mcp_config* with secrets redacted.

    Handles three secret locations:
    1. Structured ``headers`` dicts (``{"Authorization": "token ..."}``).
    2. CLI ``args`` lists (``["--header", "Authorization: token sgp_..."]``).
    3. ``env`` dicts with token-shaped values.

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

        # 1. Structured headers dict
        headers = server_cfg.get("headers")
        if isinstance(headers, dict):
            for key in list(headers):
                if key.lower() in _SENSITIVE_HEADER_NAMES:
                    if isinstance(headers[key], str):
                        # Preserve env-var references (e.g. "token ${VAR}") —
                        # they aren't secrets themselves, and redacting them
                        # breaks round-tripping through save/load.
                        if "${" in headers[key]:
                            continue
                        headers[key] = "[REDACTED]"

        # 2. CLI args list — redact "--header" value args and token-shaped args
        args = server_cfg.get("args")
        if isinstance(args, list):
            for i, arg in enumerate(args):
                if not isinstance(arg, str):
                    continue
                if "${" in arg:
                    # Env-var reference — not a secret; preserve for expansion.
                    continue
                if _AUTH_HEADER_RE.match(arg):
                    args[i] = _redact_auth_arg(arg)
                elif _is_secret(arg):
                    args[i] = "[REDACTED]"

        # 3. Env dict — redact token-shaped values
        env = server_cfg.get("env")
        if isinstance(env, dict):
            for key in list(env):
                val = env[key]
                if not isinstance(val, str):
                    continue
                if "${" in val:
                    continue
                if _is_secret(val):
                    env[key] = "[REDACTED]"

    return result
