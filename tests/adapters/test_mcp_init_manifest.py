"""Tests for the per-trial MCP init manifest (codeprobe-9p6).

The 9tk validity audit could only INFER that the Sourcegraph MCP server
attached in the ``with-sg-narrow`` arm. The fix persists the stream-json
``init`` / ``system`` event so every trial records which tools/servers were
actually offered — zero inference. These tests cover the parser, the
``AgentOutput`` field, the ``McpInitManifest`` semantics, and the on-disk
``mcp_init.json`` artifact.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from codeprobe.adapters.claude import ClaudeAdapter
from codeprobe.adapters.protocol import McpInitManifest, McpServerStatus
from codeprobe.adapters.telemetry import parse_mcp_init_manifest


def _stream(
    *,
    tools: list[str] | None = None,
    mcp_servers: list[dict] | None = None,
    include_init: bool = True,
    result_text: str = "Done.",
) -> str:
    """Build a stream-json transcript with an optional init event."""
    lines: list[str] = []
    if include_init:
        init: dict = {"type": "system", "subtype": "init"}
        if tools is not None:
            init["tools"] = tools
        if mcp_servers is not None:
            init["mcp_servers"] = mcp_servers
        lines.append(json.dumps(init))
    lines.append(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": result_text,
                "is_error": False,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 100,
                },
                "total_cost_usd": 0.05,
            }
        )
    )
    return "\n".join(lines) + "\n"


# Surface shapes mirroring the 9tk arm config (bead A3).
_NAV_TOOLS = [
    "mcp__sourcegraph__keyword_search",
    "mcp__sourcegraph__find_references",
]
_READ_TOOLS = [
    "mcp__sourcegraph__read_file",
    "mcp__sourcegraph__browse_directory",
]


class TestParseMcpInitManifest:
    def test_parses_offered_tools_and_servers(self) -> None:
        stream = _stream(
            tools=["Read", "Write", *_NAV_TOOLS],
            mcp_servers=[{"name": "sourcegraph", "status": "connected"}],
        )
        m = parse_mcp_init_manifest(stream)
        assert m.captured is True
        assert "mcp__sourcegraph__keyword_search" in m.offered_tools
        assert m.mcp_servers == (
            McpServerStatus(name="sourcegraph", status="connected"),
        )

    def test_mcp_tools_property_filters_builtins(self) -> None:
        m = parse_mcp_init_manifest(
            _stream(tools=["Read", "Bash", *_NAV_TOOLS])
        )
        # A1: the mcp__<server>__* subset is recoverable.
        assert set(m.mcp_tools) == set(_NAV_TOOLS)
        assert "Read" not in m.mcp_tools

    def test_no_init_event_is_captured_false_not_none(self) -> None:
        # A2: explicit "not measured" rather than a silent drop.
        m = parse_mcp_init_manifest(_stream(include_init=False))
        assert m.captured is False
        assert m.offered_tools == ()
        assert m.mcp_servers == ()

    def test_single_envelope_json_yields_uncaptured(self) -> None:
        envelope = json.dumps({"type": "result", "result": "ok"})
        m = parse_mcp_init_manifest(envelope)
        assert m.captured is False

    def test_failed_attach_is_recorded_not_dropped(self) -> None:
        # A2: a server that fails to attach is an explicit failed status,
        # never silently indistinguishable from "agent declined".
        stream = _stream(
            tools=["Read"],
            mcp_servers=[{"name": "sourcegraph", "status": "failed"}],
        )
        m = parse_mcp_init_manifest(stream)
        assert m.captured is True
        assert m.failed_servers == (
            McpServerStatus(name="sourcegraph", status="failed"),
        )
        # The nav tools are absent because the server never attached.
        assert m.mcp_tools == ()

    def test_pending_server_with_tools_is_not_reported_failed(self) -> None:
        """A ``pending`` HTTP server that contributed tools is healthy.

        Claude Code 2.1.220 reports HTTP MCP servers as ``pending`` in the
        init event even when they are attached and their tools return real
        results. Flagging those as failed marks every healthy HTTP arm
        invalid and buries genuine breakage.
        """
        stream = _stream(
            tools=["Read", *_NAV_TOOLS],
            mcp_servers=[{"name": "sourcegraph", "status": "pending"}],
        )
        m = parse_mcp_init_manifest(stream)
        assert m.captured is True
        assert set(m.mcp_tools) == set(_NAV_TOOLS)
        assert m.failed_servers == ()
        assert m.to_dict()["failed_servers"] == []

    def test_pending_server_without_tools_is_reported_failed(self) -> None:
        """No tools on the surface is the real breakage signal."""
        stream = _stream(
            tools=["Write"],
            mcp_servers=[{"name": "sourcegraph", "status": "pending"}],
        )
        m = parse_mcp_init_manifest(stream)
        assert m.mcp_tools == ()
        assert m.failed_servers == (
            McpServerStatus(name="sourcegraph", status="pending"),
        )

    def test_observed_http_mcp_call_reconciles_too_early_init_surface(
        self,
    ) -> None:
        init = _stream(
            tools=["ToolSearch", "Write"],
            mcp_servers=[{"name": "sourcegraph", "status": "pending"}],
        ).splitlines()
        tool_use = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__sourcegraph__keyword_search",
                            "input": {"query": "MustNoError"},
                        }
                    ]
                },
            }
        )
        stream = "\n".join([init[0], tool_use, *init[1:]]) + "\n"

        manifest = parse_mcp_init_manifest(stream)

        assert manifest.offered_tools == ("ToolSearch", "Write")
        assert manifest.observed_tools == (
            "mcp__sourcegraph__keyword_search",
        )
        assert manifest.mcp_tools == (
            "mcp__sourcegraph__keyword_search",
        )
        assert manifest.failed_servers == ()
        assert manifest.to_dict()["failed_servers"] == []

    def test_observed_tool_from_other_server_does_not_clear_failure(self) -> None:
        init = _stream(
            tools=["ToolSearch", "Write"],
            mcp_servers=[{"name": "sourcegraph", "status": "pending"}],
        ).splitlines()
        tool_use = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "mcp__other__search",
                            "input": {},
                        }
                    ]
                },
            }
        )
        stream = "\n".join([init[0], tool_use, *init[1:]]) + "\n"

        manifest = parse_mcp_init_manifest(stream)

        assert manifest.failed_servers == (
            McpServerStatus(name="sourcegraph", status="pending"),
        )

    def test_connected_server_without_tools_is_reported_failed(self) -> None:
        """A connected status is not evidence of a usable tool surface."""
        stream = _stream(
            tools=["Read"],
            mcp_servers=[{"name": "sourcegraph", "status": "connected"}],
        )
        m = parse_mcp_init_manifest(stream)

        assert m.failed_servers == (
            McpServerStatus(name="sourcegraph", status="connected"),
        )

    def test_malformed_lines_are_skipped(self) -> None:
        stream = "not json\n" + _stream(tools=["Read", *_NAV_TOOLS])
        m = parse_mcp_init_manifest(stream)
        assert m.captured is True
        assert set(m.mcp_tools) == set(_NAV_TOOLS)

    def test_bare_system_event_does_not_shadow_real_init(self) -> None:
        # A bare ``{"type": "system"}`` carrying no surface keys must not
        # match first and return an empty ``captured=True`` manifest — the
        # real init event later in the stream must win.
        bare = json.dumps({"type": "system"})
        stream = bare + "\n" + _stream(tools=["Read", *_NAV_TOOLS])
        m = parse_mcp_init_manifest(stream)
        assert m.captured is True
        assert set(m.mcp_tools) == set(_NAV_TOOLS)


class TestToDict:
    def test_to_dict_shape(self) -> None:
        m = McpInitManifest(
            captured=True,
            offered_tools=("Read", *_NAV_TOOLS),
            observed_tools=("mcp__sourcegraph__keyword_search",),
            mcp_servers=(
                McpServerStatus(name="sourcegraph", status="connected"),
                McpServerStatus(name="broken", status="failed"),
            ),
        )
        d = m.to_dict()
        assert d["captured"] is True
        assert d["offered_tools"] == ["Read", *_NAV_TOOLS]
        assert d["observed_tools"] == [
            "mcp__sourcegraph__keyword_search"
        ]
        assert set(d["mcp_tools"]) == set(_NAV_TOOLS)
        assert d["mcp_servers"] == [
            {"name": "sourcegraph", "status": "connected"},
            {"name": "broken", "status": "failed"},
        ]
        assert d["failed_servers"] == ["broken"]


class TestParseOutputThreadsManifest:
    def _parse(self, stream: str):
        adapter = ClaudeAdapter()
        result = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout=stream, stderr=""
        )
        return adapter.parse_output(result, duration=1.0)

    def test_parse_output_sets_mcp_init(self) -> None:
        out = self._parse(
            _stream(
                tools=["Read", *_NAV_TOOLS],
                mcp_servers=[{"name": "sourcegraph", "status": "connected"}],
            )
        )
        assert out.mcp_init is not None
        assert out.mcp_init.captured is True
        assert set(out.mcp_init.mcp_tools) == set(_NAV_TOOLS)

    def test_narrow_vs_full_surface(self) -> None:
        # A3: narrow arm offers nav/search SG tools but not read/browse;
        # full arm offers both. Proven from the artifact, no inference.
        narrow = self._parse(
            _stream(tools=["Read", *_NAV_TOOLS])
        ).mcp_init
        full = self._parse(
            _stream(tools=["Read", *_NAV_TOOLS, *_READ_TOOLS])
        ).mcp_init
        assert set(narrow.mcp_tools) == set(_NAV_TOOLS)
        for read_tool in _READ_TOOLS:
            assert read_tool not in narrow.mcp_tools
            assert read_tool in full.mcp_tools

    def test_uncaptured_when_quota_stub(self) -> None:
        # A non-stream stub (e.g. quota) yields an explicit uncaptured
        # manifest, never None-by-accident — A4 (no score impact) holds
        # because this is additive telemetry only.
        out = self._parse("You have reached your monthly usage limit.")
        assert out.mcp_init is not None
        assert out.mcp_init.captured is False


class TestArtifactWrite:
    def test_save_task_artifacts_writes_mcp_init_json(self, tmp_path: Path) -> None:
        from codeprobe.core.executor import TaskResult, _save_task_artifacts
        from codeprobe.models.experiment import CompletedTask

        manifest = McpInitManifest(
            captured=True,
            offered_tools=("Read", *_NAV_TOOLS),
            mcp_servers=(McpServerStatus(name="sourcegraph", status="connected"),),
        )
        completed = CompletedTask(
            task_id="t1",
            automated_score=1.0,
            status="completed",
            mcp_init=manifest.to_dict(),
        )
        _save_task_artifacts(
            tmp_path, "t1", TaskResult(completed=completed, agent_stdout="x")
        )
        artifact = tmp_path / "t1" / "mcp_init.json"
        assert artifact.is_file()
        data = json.loads(artifact.read_text())
        assert data["captured"] is True
        assert set(data["mcp_tools"]) == set(_NAV_TOOLS)

    def test_no_artifact_when_manifest_absent(self, tmp_path: Path) -> None:
        from codeprobe.core.executor import TaskResult, _save_task_artifacts
        from codeprobe.models.experiment import CompletedTask

        completed = CompletedTask(task_id="t2", automated_score=0.0, mcp_init=None)
        _save_task_artifacts(
            tmp_path, "t2", TaskResult(completed=completed, agent_stdout="x")
        )
        assert not (tmp_path / "t2" / "mcp_init.json").exists()
