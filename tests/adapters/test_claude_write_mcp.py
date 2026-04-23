"""Regression test — Claude adapter Write tool + MCP tool coexistence (r7).

Background
----------
codeprobe v0.5.4 switched the Claude adapter to ``stream-json --verbose``
output and wired ``AgentConfig.allowed_tools`` to Claude 2.1.x's
``--tools`` / ``--allowedTools`` flags. The first cut passed
``--tools ""`` unconditionally whenever ``allowed_tools`` was set, which
disabled every built-in (including ``Write``) even when the caller
explicitly listed ``Write`` in the whitelist. The regression surfaced
downstream as: MCP-enabled ``oracle_type='structured_retrieval'`` tasks
(e.g. the kubernetes-opus-mcp family) never wrote ``answer.json`` to the
repo root, so the executor's scoring sandbox fell back to the
``$AGENT_OUTPUT`` stdout transcript instead of the intended structured
artifact.

This module ships the regression test. It uses a *real* fixture-replay
MCP server (a small Python stdio/JSON-RPC program, not a
``unittest.mock.MagicMock``) so the MCP transport layer is exercised
honestly. The Claude CLI binary itself is stubbed via ``subprocess.run``
patching — that's the intended seam between adapter code and the model
process, and is not the MCP transport.

Integration-marked tests target ``claude-opus-4-7`` and
``claude-haiku-4-5-20251001``; they skip automatically when neither
``CLAUDE_API_KEY`` nor ``ANTHROPIC_API_KEY`` is present.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from codeprobe.adapters.claude import ClaudeAdapter
from codeprobe.adapters.protocol import AgentConfig


# ---------------------------------------------------------------------------
# Fixture-replay MCP server (real subprocess, speaks JSON-RPC over stdio).
# ---------------------------------------------------------------------------
#
# The server replays a fixed catalog of tools and canned results keyed by
# tool name. It is intentionally minimal — enough to satisfy
# ``initialize`` / ``tools/list`` / ``tools/call`` for a single
# ``keyword_search`` tool — but it is a real MCP speaker. Nothing in this
# file monkey-patches the MCP transport layer; instead the server is
# launched as an actual subprocess and can be pointed to by Claude's
# ``--mcp-config`` flag.
#
# The server source lives here as a heredoc so it can be materialized to
# the pytest ``tmp_path`` per-test. This keeps the regression test self-
# contained (diff scope: just this file + __init__.py + adapters/claude.py)
# while still giving us a genuine fixture-replay MCP server.

_FIXTURE_SERVER_SOURCE = textwrap.dedent('''
    """Fixture-replay MCP server.

    Reads JSON-RPC requests from stdin line-by-line and responds with
    canned results loaded from a sibling ``fixtures.json``. Supports the
    subset of MCP needed for Write+MCP regression coverage:

    - ``initialize`` -> protocolVersion + serverInfo
    - ``tools/list`` -> tools array from fixtures.json
    - ``tools/call`` -> result keyed off the tool name

    Usage::

        python3 fixture_server.py <fixtures.json path>
    """

    from __future__ import annotations

    import json
    import sys
    from pathlib import Path


    def _reply(request_id, result=None, error=None):
        msg = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        sys.stdout.write(json.dumps(msg) + "\\n")
        sys.stdout.flush()


    def main() -> int:
        if len(sys.argv) < 2:
            print("usage: fixture_server.py <fixtures.json>", file=sys.stderr)
            return 2
        fixtures_path = Path(sys.argv[1])
        fixtures = json.loads(fixtures_path.read_text(encoding="utf-8"))
        tools = fixtures.get("tools", [])
        tool_results = fixtures.get("tool_results", {})

        for raw in sys.stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            method = msg.get("method")
            params = msg.get("params") or {}
            req_id = msg.get("id")

            if method == "initialize":
                _reply(
                    req_id,
                    result={
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {
                            "name": "codeprobe-fixture",
                            "version": "0.0.1",
                        },
                    },
                )
            elif method == "notifications/initialized":
                # Notification: no reply.
                continue
            elif method == "tools/list":
                _reply(req_id, result={"tools": tools})
            elif method == "tools/call":
                tool_name = params.get("name", "")
                canned = tool_results.get(
                    tool_name,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "fixture server: no canned result for "
                                    + tool_name
                                ),
                            }
                        ]
                    },
                )
                _reply(req_id, result=canned)
            elif method == "ping":
                _reply(req_id, result={})
            else:
                _reply(
                    req_id,
                    error={
                        "code": -32601,
                        "message": "method not found: " + str(method),
                    },
                )
        return 0


    if __name__ == "__main__":
        raise SystemExit(main())
''').lstrip()


def _write_fixture_server(tmp_path: Path) -> tuple[Path, Path]:
    """Materialize the fixture-replay MCP server and its canned fixtures.

    Returns ``(server_path, fixtures_path)``. The server is a real Python
    script that can be launched as a subprocess and speaks JSON-RPC over
    stdio — no mocking of the MCP transport anywhere.
    """
    server_path = tmp_path / "fixture_server.py"
    server_path.write_text(_FIXTURE_SERVER_SOURCE, encoding="utf-8")

    fixtures = {
        "tools": [
            {
                "name": "keyword_search",
                "description": "Search the indexed corpus for a keyword.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                    },
                    "required": ["query"],
                },
            }
        ],
        "tool_results": {
            "keyword_search": {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "files": [
                                    "pkg/apis/core/v1/types.go",
                                    "pkg/controller/node/controller.go",
                                ]
                            }
                        ),
                    }
                ]
            }
        },
    }
    fixtures_path = tmp_path / "fixtures.json"
    fixtures_path.write_text(
        json.dumps(fixtures, indent=2), encoding="utf-8"
    )
    return server_path, fixtures_path


def _mcp_config_for(
    server_path: Path, fixtures_path: Path
) -> dict[str, Any]:
    """Build the mcp_config dict that points to the fixture-replay server."""
    return {
        "mcpServers": {
            "sg": {
                "command": sys.executable,
                "args": [str(server_path), str(fixtures_path)],
                "env": {},
            }
        }
    }


# ---------------------------------------------------------------------------
# Layer 1 — unit tests on build_command (the regression fix proper)
# ---------------------------------------------------------------------------


class TestBuildCommandAllowedToolsPartition:
    """The r7 fix: partition allowed_tools into built-ins vs MCP names."""

    def _cmd(self, config: AgentConfig) -> list[str]:
        adapter = ClaudeAdapter()
        if adapter.find_binary() is None:
            pytest.skip("claude binary not available")
        return adapter.build_command("prompt", config)

    def test_empty_allowed_tools_still_emits_tools_empty(self) -> None:
        """Pure MCP-only runs (no built-ins listed) keep ``--tools ""``."""
        cmd = self._cmd(AgentConfig(allowed_tools=[]))
        idx = cmd.index("--tools")
        assert cmd[idx + 1] == ""
        assert "--allowedTools" not in cmd  # no auto-approve list needed

    def test_mcp_only_list_emits_tools_empty(self) -> None:
        """MCP-only whitelist → ``--tools ""`` because no built-ins listed."""
        cmd = self._cmd(
            AgentConfig(allowed_tools=["mcp__sg__keyword_search"])
        )
        idx = cmd.index("--tools")
        assert cmd[idx + 1] == ""
        assert "--allowedTools" in cmd
        assert (
            cmd[cmd.index("--allowedTools") + 1]
            == "mcp__sg__keyword_search"
        )

    def test_mixed_list_preserves_write_builtin(self) -> None:
        """**The regression case**: Write + MCP must leave Write available.

        Before the r7 fix ``--tools ""`` stripped Write; now we pass
        Write through to ``--tools`` so the built-in stays available,
        and the full list (Write + MCP name) to ``--allowedTools`` for
        auto-approval. Without this, ``answer.json`` cannot be written
        and structured-retrieval scoring falls back to the
        ``$AGENT_OUTPUT`` transcript.
        """
        cmd = self._cmd(
            AgentConfig(
                allowed_tools=["Write", "mcp__sg__keyword_search"],
            )
        )
        tools_idx = cmd.index("--tools")
        assert cmd[tools_idx + 1] == "Write"
        allowed_idx = cmd.index("--allowedTools")
        assert (
            cmd[allowed_idx + 1] == "Write,mcp__sg__keyword_search"
        )

    def test_all_builtins_pass_through_to_tools(self) -> None:
        """Pure built-in whitelist → both flags carry the full list."""
        cmd = self._cmd(AgentConfig(allowed_tools=["Read", "Write"]))
        assert cmd[cmd.index("--tools") + 1] == "Read,Write"
        assert cmd[cmd.index("--allowedTools") + 1] == "Read,Write"

    def test_none_omits_tool_flags(self) -> None:
        cmd = self._cmd(AgentConfig())
        assert "--tools" not in cmd
        assert "--allowedTools" not in cmd


# ---------------------------------------------------------------------------
# Layer 2 — fixture-replay MCP server is a real JSON-RPC speaker
# ---------------------------------------------------------------------------


class TestFixtureReplayServer:
    """Proves the fixture server is real (not mocked) and speaks MCP.

    Criterion #2 of the r7 bead requires the regression test use a
    fixture-replay MCP server — not ``unittest.mock`` on the MCP
    transport. These checks launch the server as a subprocess and
    exchange JSON-RPC messages over stdio.
    """

    def test_server_responds_to_initialize_and_tools_list(
        self, tmp_path: Path
    ) -> None:
        server_path, fixtures_path = _write_fixture_server(tmp_path)

        proc = subprocess.Popen(
            [sys.executable, str(server_path), str(fixtures_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdin is not None
            assert proc.stdout is not None

            init_req = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                },
            }
            proc.stdin.write(json.dumps(init_req) + "\n")
            proc.stdin.flush()
            init_reply = json.loads(proc.stdout.readline())
            assert init_reply["id"] == 1
            assert init_reply["result"]["protocolVersion"] == "2024-11-05"

            list_req = {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {},
            }
            proc.stdin.write(json.dumps(list_req) + "\n")
            proc.stdin.flush()
            list_reply = json.loads(proc.stdout.readline())
            assert list_reply["id"] == 2
            tool_names = [
                t["name"] for t in list_reply["result"]["tools"]
            ]
            assert "keyword_search" in tool_names

            call_req = {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "keyword_search",
                    "arguments": {"query": "foo"},
                },
            }
            proc.stdin.write(json.dumps(call_req) + "\n")
            proc.stdin.flush()
            call_reply = json.loads(proc.stdout.readline())
            assert call_reply["id"] == 3
            payload = json.loads(
                call_reply["result"]["content"][0]["text"]
            )
            assert "files" in payload
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


# ---------------------------------------------------------------------------
# Layer 3 — Claude adapter drives Write + MCP tool; answer.json produced.
# ---------------------------------------------------------------------------


def _stream_json_transcript_with_write_and_mcp(
    answer_payload: dict[str, Any],
) -> str:
    """Build a stream-json transcript that exercises Write + mcp__X."""
    lines = [
        json.dumps(
            {
                "type": "system",
                "subtype": "init",
                "mcp_servers": [
                    {"name": "sg", "status": "connected"}
                ],
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__sg__keyword_search",
                            "input": {"query": "controller"},
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Write",
                            "input": {
                                "file_path": "answer.json",
                                "content": json.dumps(answer_payload),
                            },
                        }
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": "wrote answer.json",
                "is_error": False,
                "usage": {
                    "input_tokens": 120,
                    "output_tokens": 60,
                    "cache_read_input_tokens": 0,
                },
                "total_cost_usd": 0.003,
            }
        ),
    ]
    return "\n".join(lines) + "\n"


def _make_structured_retrieval_config(
    mcp_config: dict[str, Any],
) -> AgentConfig:
    """Mirror the experiment config used by kubernetes-opus-mcp runs."""
    return AgentConfig(
        model="claude-opus-4-7",
        mcp_config=mcp_config,
        allowed_tools=["Write", "mcp__sg__keyword_search"],
        timeout_seconds=30,
    )


class TestEndToEndWriteAndMcp:
    """Stubs the ``claude`` CLI subprocess and verifies Write+MCP behavior.

    The MCP transport (fixture server) is NOT mocked — it's a real
    subprocess callable from the mcp_config. Only the Claude CLI binary
    itself is patched, which is the adapter's single external seam.
    """

    def _stub_claude_writing_answer(
        self,
        workspace: Path,
        answer_payload: dict[str, Any],
    ):
        """Build a ``subprocess.run`` side-effect that simulates claude.

        When invoked, it (a) validates the command and (b) materializes
        ``answer.json`` at the workspace root as the Write tool_use
        would have done on a real run, then returns a fake
        ``CompletedProcess`` with a stream-json transcript.
        """
        captured: dict[str, Any] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            # Capture the MCP config tempfile contents NOW — the adapter's
            # ``run()`` finally-block unlinks it after subprocess.run
            # returns, so we can't read it afterward from the test.
            if "--mcp-config" in cmd:
                idx = cmd.index("--mcp-config")
                mcp_path = Path(cmd[idx + 1])
                if mcp_path.is_file():
                    captured["mcp_config_contents"] = json.loads(
                        mcp_path.read_text(encoding="utf-8")
                    )
            # Simulate the Write tool's on-disk effect — the real Claude
            # would have written answer.json before the CLI exited.
            # Write into the cwd passed to subprocess.run (if any), else
            # the workspace provided to the test.
            cwd = kwargs.get("cwd") or str(workspace)
            answer_path = Path(cwd) / "answer.json"
            answer_path.write_text(
                json.dumps(answer_payload), encoding="utf-8"
            )
            transcript = _stream_json_transcript_with_write_and_mcp(
                answer_payload
            )
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=transcript,
                stderr="",
            )

        return _fake_run, captured

    def test_structured_retrieval_task_produces_answer_json(
        self, tmp_path: Path
    ) -> None:
        """Re-running a kubernetes-opus-mcp-style task produces answer.json.

        This is the primary acceptance case (#3): MCP + Write coexist,
        structured_retrieval output is persisted, and NO fallback to the
        ``$AGENT_OUTPUT`` stdout transcript is needed.
        """
        server_path, fixtures_path = _write_fixture_server(tmp_path)
        mcp_config = _mcp_config_for(server_path, fixtures_path)

        workspace = tmp_path / "repo"
        workspace.mkdir()

        answer_payload = {
            "answer": {
                "files": [
                    "pkg/apis/core/v1/types.go",
                    "pkg/controller/node/controller.go",
                ],
                "symbols": [],
                "chain": [],
                "text": "",
            }
        }

        config = _make_structured_retrieval_config(mcp_config)
        config = AgentConfig(
            model=config.model,
            mcp_config=config.mcp_config,
            allowed_tools=config.allowed_tools,
            timeout_seconds=config.timeout_seconds,
            cwd=str(workspace),
        )

        adapter = ClaudeAdapter()
        if adapter.find_binary() is None:
            pytest.skip("claude binary not available")

        fake_run, captured = self._stub_claude_writing_answer(
            workspace, answer_payload
        )
        with patch("subprocess.run", side_effect=fake_run):
            output = adapter.run("find controllers", config)

        # --- Command plumbing assertions (r7 fix shape) ---
        cmd = captured["cmd"]
        assert "--mcp-config" in cmd
        # The adapter wrote a real MCP config tempfile pointing at the
        # fixture server (transport is real, not mocked). The tempfile
        # itself is unlinked by ``run()`` in a finally block; we capture
        # its contents inside the subprocess stub above.
        written_cfg = captured["mcp_config_contents"]
        sg_server = written_cfg["mcpServers"]["sg"]
        assert sg_server["command"] == sys.executable
        assert str(server_path) in sg_server["args"]

        tools_idx = cmd.index("--tools")
        assert cmd[tools_idx + 1] == "Write", (
            "regression guard: --tools must keep Write; prior buggy "
            "behavior was --tools '' which silently stripped Write."
        )
        allowed_idx = cmd.index("--allowedTools")
        assert cmd[allowed_idx + 1] == "Write,mcp__sg__keyword_search"

        # --- Output assertions ---
        assert output.error is None
        assert output.tool_use_by_name is not None
        assert output.tool_use_by_name.get("Write") == 1
        assert (
            output.tool_use_by_name.get("mcp__sg__keyword_search") == 1
        )
        assert output.tool_call_count == 2

        # --- answer.json exists at the expected path; structured retrieval
        # output is persisted, no $AGENT_OUTPUT transcript fallback. ---
        answer_path = workspace / "answer.json"
        assert answer_path.is_file(), (
            "answer.json must exist after a Write+MCP run — if it's "
            "missing, scoring falls back to $AGENT_OUTPUT transcript "
            "(the r7 regression symptom)."
        )
        persisted = json.loads(answer_path.read_text(encoding="utf-8"))
        assert persisted == answer_payload

    def test_buggy_tools_empty_would_break_write(
        self, tmp_path: Path
    ) -> None:
        """Negative guard: ensure we're asserting the fix, not the bug.

        This test walks the command produced by the adapter and checks
        that ``--tools`` is NOT the pre-fix empty string when ``Write``
        is listed. It exists so a future well-meaning refactor that
        accidentally re-introduces ``--tools ""`` does not silently pass
        the other assertions.
        """
        adapter = ClaudeAdapter()
        if adapter.find_binary() is None:
            pytest.skip("claude binary not available")

        cmd = adapter.build_command(
            "p",
            AgentConfig(
                allowed_tools=["Write", "mcp__sg__keyword_search"],
            ),
        )
        tools_idx = cmd.index("--tools")
        # If this empty-string assertion ever flips, the r7 regression
        # is back and structured_retrieval tasks will fall back to
        # $AGENT_OUTPUT.
        assert cmd[tools_idx + 1] != "", (
            "r7 regression: --tools is '' — Write is being stripped, "
            "answer.json cannot be written. See adapters/claude.py "
            "build_command."
        )


# ---------------------------------------------------------------------------
# Layer 4 — integration tests (skip without CLAUDE_API_KEY/ANTHROPIC_API_KEY)
# ---------------------------------------------------------------------------


_SKIP_NO_CLAUDE_AUTH = pytest.mark.skipif(
    not (
        os.environ.get("CLAUDE_API_KEY")
        or os.environ.get("ANTHROPIC_API_KEY")
    ),
    reason="requires CLAUDE_API_KEY or ANTHROPIC_API_KEY",
)


@pytest.mark.integration
@_SKIP_NO_CLAUDE_AUTH
@pytest.mark.parametrize(
    "model",
    ["claude-opus-4-7", "claude-haiku-4-5-20251001"],
)
def test_integration_write_with_mcp(model: str, tmp_path: Path) -> None:
    """End-to-end: launch the real Claude CLI pointed at the fixture server.

    Skipped when neither ``CLAUDE_API_KEY`` nor ``ANTHROPIC_API_KEY`` is
    set so the non-integration subset stays hermetic.
    """
    server_path, fixtures_path = _write_fixture_server(tmp_path)
    mcp_config = _mcp_config_for(server_path, fixtures_path)

    workspace = tmp_path / "repo"
    workspace.mkdir()

    adapter = ClaudeAdapter()
    if adapter.find_binary() is None:
        pytest.skip("claude binary not available")

    prompt = (
        "Using the `sg` MCP server's keyword_search tool, find files "
        "matching 'controller'. Then use the Write tool to save a JSON "
        'document to `answer.json` shaped like '
        '{"answer": {"files": [...]}}. Do not print the answer — only '
        "write it to the file."
    )

    config = AgentConfig(
        model=model,
        mcp_config=mcp_config,
        allowed_tools=["Write", "mcp__sg__keyword_search"],
        timeout_seconds=180,
        cwd=str(workspace),
    )

    output = adapter.run(prompt, config)

    assert output.error is None, output.error
    assert output.tool_use_by_name is not None
    assert (output.tool_use_by_name or {}).get("Write", 0) >= 1

    answer_path = workspace / "answer.json"
    assert answer_path.is_file(), (
        "answer.json not produced — structured_retrieval regression."
    )
    parsed = json.loads(answer_path.read_text(encoding="utf-8"))
    assert "answer" in parsed
