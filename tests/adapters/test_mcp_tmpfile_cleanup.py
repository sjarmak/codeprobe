"""MCP config temp-file cleanup across adapters (codeprobe-f7rl.39).

``BaseAdapter._write_mcp_config`` expands ``${VAR}`` references from the
environment BEFORE writing, so the temp file holds real secrets. The
cleanup scan in ``BaseAdapter.run`` used to match the flag value against
``tempfile.gettempdir()`` verbatim, but Copilot passes the path as
``@{mcp_path}`` (the CLI's file-reference convention), so the check never
matched and the secret-bearing file was never unlinked. These tests lock
in cleanup for the @-prefixed (Copilot), bare (Claude), and timeout paths.

The agent CLI binary is stubbed at the adapter process-runner seam and
``find_binary`` is patched so the tests are hermetic — no copilot/claude
install required.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from codeprobe.adapters.claude import ClaudeAdapter
from codeprobe.adapters.copilot import CopilotAdapter
from codeprobe.adapters.protocol import AgentConfig

_SECRET = "hunter2secret"


def _mcp_config_with_env_ref() -> dict[str, Any]:
    """MCP config whose header references a secret via ${FAKE_TOKEN}."""
    return {
        "mcpServers": {
            "gh": {
                "type": "http",
                "url": "https://api.example.test/mcp",
                "headers": {"Authorization": "token ${FAKE_TOKEN}"},
            }
        }
    }


def _extract_mcp_path(cmd: list[str]) -> str:
    """Pull the MCP config file path out of the built command.

    Handles both Claude's bare ``--mcp-config <path>`` and Copilot's
    ``--additional-mcp-config @<path>`` conventions.
    """
    for flag in ("--mcp-config", "--additional-mcp-config"):
        if flag in cmd:
            value = cmd[cmd.index(flag) + 1]
            return value[1:] if value.startswith("@") else value
    raise AssertionError(f"no MCP config flag in command: {cmd}")


def _capturing_fake_run(captured: dict[str, Any]):
    """Adapter process-runner stand-in: records cmd and the temp file body.

    The body must be read INSIDE the stub — run()'s finally block unlinks
    the file right after the adapter runner returns.
    """

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        mcp_path = _extract_mcp_path(cmd)
        captured["mcp_body"] = Path(mcp_path).read_text(encoding="utf-8")
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    return _fake_run


@pytest.fixture()
def fake_token_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_TOKEN", _SECRET)


def test_copilot_mcp_tmpfile_deleted_after_run(
    monkeypatch: pytest.MonkeyPatch, fake_token_env: None
) -> None:
    """The @-prefixed --additional-mcp-config path is unlinked after run().

    Pre-fix, the tempdir check compared the raw ``@/tmp/...`` value, never
    matched, and the expanded-secret file leaked (162 observed on the
    audit host).
    """
    monkeypatch.setattr(CopilotAdapter, "find_binary", lambda self: "/fake/copilot")
    adapter = CopilotAdapter()
    config = AgentConfig(mcp_config=_mcp_config_with_env_ref(), timeout_seconds=5)

    captured: dict[str, Any] = {}
    with patch(
        "codeprobe.adapters._base._run_agent_process",
        side_effect=_capturing_fake_run(captured),
    ):
        adapter.run("prompt", config)

    flag_value = captured["cmd"][captured["cmd"].index("--additional-mcp-config") + 1]
    assert flag_value.startswith("@"), "Copilot must keep the CLI's @-prefix contract"
    mcp_path = flag_value[1:]
    # Premise: the file held the EXPANDED secret while the agent ran.
    assert _SECRET in captured["mcp_body"]
    assert "${FAKE_TOKEN}" not in captured["mcp_body"]
    # The fix: cleanup normalizes the @-prefix and unlinks the file.
    assert not os.path.exists(mcp_path)


def test_claude_mcp_tmpfile_deleted_after_run(
    monkeypatch: pytest.MonkeyPatch, fake_token_env: None
) -> None:
    """Regression guard: bare --mcp-config paths are still cleaned up."""
    monkeypatch.setattr(ClaudeAdapter, "find_binary", lambda self: "/fake/claude")
    adapter = ClaudeAdapter()
    config = AgentConfig(mcp_config=_mcp_config_with_env_ref(), timeout_seconds=5)

    captured: dict[str, Any] = {}
    with patch(
        "codeprobe.adapters._base._run_agent_process",
        side_effect=_capturing_fake_run(captured),
    ):
        adapter.run("prompt", config)

    flag_value = captured["cmd"][captured["cmd"].index("--mcp-config") + 1]
    assert not flag_value.startswith("@")
    assert _SECRET in captured["mcp_body"]
    assert not os.path.exists(flag_value)


def test_copilot_mcp_tmpfile_deleted_on_timeout(
    monkeypatch: pytest.MonkeyPatch, fake_token_env: None
) -> None:
    """The finally block unlinks the @-prefixed temp file on timeout too."""
    monkeypatch.setattr(CopilotAdapter, "find_binary", lambda self: "/fake/copilot")
    adapter = CopilotAdapter()
    config = AgentConfig(mcp_config=_mcp_config_with_env_ref(), timeout_seconds=5)

    captured: dict[str, Any] = {}

    def _timing_out_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["mcp_path"] = _extract_mcp_path(cmd)
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)

    with patch(
        "codeprobe.adapters._base._run_agent_process",
        side_effect=_timing_out_run,
    ):
        output = adapter.run("prompt", config)

    assert output.error_category == "timeout"
    assert not os.path.exists(captured["mcp_path"])
