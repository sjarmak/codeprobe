"""Runtime validation for secret-bearing MCP configuration."""

from __future__ import annotations

from copy import deepcopy

import pytest

from codeprobe.config.mcp_runtime import (
    MCPConfigCredentialError,
    resolve_mcp_runtime_config,
)


def _config(authorization: str) -> dict:
    return {
        "mcpServers": {
            "sourcegraph": {
                "type": "http",
                "url": "https://sourcegraph.example/.api/mcp/all",
                "headers": {"Authorization": authorization},
            }
        }
    }


def test_rejects_redacted_runtime_credential() -> None:
    with pytest.raises(MCPConfigCredentialError, match="redacted"):
        resolve_mcp_runtime_config(_config("[REDACTED]"), environ={})


@pytest.mark.parametrize(
    ("reference", "variable"),
    [
        ("token ${SOURCEGRAPH_TOKEN}", "SOURCEGRAPH_TOKEN"),
        ("token $SOURCEGRAPH_TOKEN", "SOURCEGRAPH_TOKEN"),
    ],
)
def test_rejects_unresolved_environment_reference(
    reference: str,
    variable: str,
) -> None:
    with pytest.raises(MCPConfigCredentialError, match=variable):
        resolve_mcp_runtime_config(_config(reference), environ={})


def test_expands_environment_without_mutating_source() -> None:
    source = _config("token ${SOURCEGRAPH_TOKEN}")
    original = deepcopy(source)

    resolved = resolve_mcp_runtime_config(
        source,
        environ={"SOURCEGRAPH_TOKEN": 'sgp_quote-"safe'},
    )

    assert source == original
    assert (
        resolved["mcpServers"]["sourcegraph"]["headers"]["Authorization"]
        == 'token sgp_quote-"safe'
    )


def test_expands_references_in_server_args_and_env() -> None:
    source = {
        "mcpServers": {
            "local": {
                "command": "server",
                "args": ["--token", "$TOKEN"],
                "env": {"API_TOKEN": "${TOKEN}"},
            }
        }
    }

    resolved = resolve_mcp_runtime_config(
        source,
        environ={"TOKEN": "secret"},
    )

    assert resolved["mcpServers"]["local"]["args"] == ["--token", "secret"]
    assert resolved["mcpServers"]["local"]["env"] == {"API_TOKEN": "secret"}


def test_error_never_contains_environment_value() -> None:
    secret = "sgp_do-not-print-me"
    source = _config("[REDACTED]")

    with pytest.raises(MCPConfigCredentialError) as caught:
        resolve_mcp_runtime_config(
            source,
            environ={"SOURCEGRAPH_TOKEN": secret},
        )

    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    ("environment_value", "expected"),
    [
        ("[REDACTED]", "redacted"),
        ("token ${STILL_MISSING}", "STILL_MISSING"),
    ],
)
def test_rejects_unusable_value_introduced_by_environment_expansion(
    environment_value: str,
    expected: str,
) -> None:
    source = _config("${SOURCEGRAPH_TOKEN}")

    with pytest.raises(MCPConfigCredentialError, match=expected):
        resolve_mcp_runtime_config(
            source,
            environ={"SOURCEGRAPH_TOKEN": environment_value},
        )
