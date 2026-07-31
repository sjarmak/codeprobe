"""Resolve MCP configuration without leaking or dispatching bad credentials."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, cast

_REDACTED_MARKER = "[REDACTED]"
_ENV_REFERENCE_RE = re.compile(
    r"\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|"
    r"(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
)


class MCPConfigCredentialError(ValueError):
    """An MCP config contains a credential that cannot work at runtime."""


def _display_path(path: tuple[str, ...]) -> str:
    return ".".join(path) if path else "<root>"


def _resolve_value(
    value: Any,
    path: tuple[str, ...],
    environ: Mapping[str, str],
    redacted_paths: set[str],
    unresolved_variables: set[str],
) -> Any:
    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            variable = match.group("braced") or match.group("bare")
            if variable not in environ:
                unresolved_variables.add(variable)
                return match.group(0)
            return environ[variable]

        resolved = _ENV_REFERENCE_RE.sub(_replace, value)
        if _REDACTED_MARKER in resolved:
            redacted_paths.add(_display_path(path))
        for match in _ENV_REFERENCE_RE.finditer(resolved):
            unresolved_variables.add(match.group("braced") or match.group("bare"))
        return resolved
    if isinstance(value, list):
        return [
            _resolve_value(
                item,
                (*path, str(index)),
                environ,
                redacted_paths,
                unresolved_variables,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        return {
            key: _resolve_value(
                item,
                (*path, str(key)),
                environ,
                redacted_paths,
                unresolved_variables,
            )
            for key, item in value.items()
        }
    return value


def resolve_mcp_runtime_config(
    mcp_config: dict[str, Any] | None,
    *,
    environ: Mapping[str, str],
) -> dict[str, Any] | None:
    """Return an expanded copy or reject redacted/unresolved values.

    Expansion walks values directly instead of serializing through JSON, so
    quotes and control characters in environment values cannot corrupt the
    config document. The input is never mutated and secret values never appear
    in diagnostics.
    """
    if mcp_config is None:
        return None
    if not isinstance(mcp_config, dict):
        raise MCPConfigCredentialError("MCP config must be a JSON object")

    redacted_paths: set[str] = set()
    unresolved_variables: set[str] = set()
    resolved = cast(
        dict[str, Any],
        _resolve_value(
            mcp_config,
            (),
            environ,
            redacted_paths,
            unresolved_variables,
        ),
    )
    issues: list[str] = []
    if redacted_paths:
        issues.append(
            "redacted value at " + ", ".join(sorted(redacted_paths))
        )
    if unresolved_variables:
        issues.append(
            "unresolved environment variable(s): "
            + ", ".join(sorted(unresolved_variables))
        )
    if issues:
        raise MCPConfigCredentialError("; ".join(issues))
    return resolved


__all__ = [
    "MCPConfigCredentialError",
    "resolve_mcp_runtime_config",
]
