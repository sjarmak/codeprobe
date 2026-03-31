"""Tests for SessionCollector protocol and implementations."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.adapters.protocol import AgentConfig
from codeprobe.adapters.session import (
    ClaudeSessionCollector,
    CodexSessionCollector,
    CopilotSessionCollector,
    SessionCollector,
    _calculate_cost,
    _find_session,
    _sum_usage,
)
from codeprobe.core.registry import available_sessions, resolve_session

# -- Helpers -------------------------------------------------------------------


def _write_session(
    sessions_dir: Path, pid: int, session_id: str, cwd: str, started_at: int = 1000
) -> None:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    (sessions_dir / f"{pid}.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "sessionId": session_id,
                "cwd": cwd,
                "startedAt": started_at,
                "kind": "interactive",
                "entrypoint": "cli",
            }
        )
    )


def _write_jsonl(jsonl_path: Path, entries: list[dict]) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(e) for e in entries]
    jsonl_path.write_text("\n".join(lines) + "\n")


def _assistant_entry(
    input_tokens: int = 100,
    output_tokens: int = 50,
    cache_read: int = 10,
    cache_creation: int = 5,
) -> dict:
    return {
        "type": "assistant",
        "message": {
            "model": "claude-sonnet-4-6",
            "role": "assistant",
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
            },
        },
    }


def _user_entry() -> dict:
    return {"type": "user", "message": {"role": "user", "content": "hello"}}


# -- Protocol conformance -----------------------------------------------------


def test_session_collector_protocol_conformance():
    assert isinstance(ClaudeSessionCollector(), SessionCollector)
    assert isinstance(CopilotSessionCollector(), SessionCollector)
    assert isinstance(CodexSessionCollector(), SessionCollector)


# -- _find_session tests -------------------------------------------------------


def test_find_session_by_cwd(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    _write_session(sessions_dir, 100, "sess-1", "/my/project", started_at=500)
    _write_session(sessions_dir, 200, "sess-2", "/my/project", started_at=1000)
    _write_session(sessions_dir, 300, "sess-3", "/other/project")

    result = _find_session(sessions_dir, "/my/project")
    assert result is not None
    assert result["sessionId"] == "sess-2"  # highest startedAt


def test_find_session_by_id(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    _write_session(sessions_dir, 100, "sess-1", "/my/project")

    result = _find_session(sessions_dir, "/other", session_id="sess-1")
    assert result is not None
    assert result["sessionId"] == "sess-1"


def test_find_session_not_found(tmp_path: Path):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir(parents=True)
    assert _find_session(sessions_dir, "/nonexistent") is None


def test_find_session_no_dir(tmp_path: Path):
    assert _find_session(tmp_path / "nope", "/anything") is None


# -- _sum_usage tests ----------------------------------------------------------


def test_sum_usage_multiple_entries(tmp_path: Path):
    jsonl = tmp_path / "session.jsonl"
    _write_jsonl(
        jsonl,
        [
            _assistant_entry(100, 50, 10, 5),
            _user_entry(),
            _assistant_entry(200, 100, 20, 10),
        ],
    )

    totals = _sum_usage(jsonl)
    assert totals.input_tokens == 300
    assert totals.output_tokens == 150
    assert totals.cache_read_tokens == 30
    assert totals.cache_creation_tokens == 15
    assert totals.entry_count == 2


def test_sum_usage_empty_file(tmp_path: Path):
    jsonl = tmp_path / "session.jsonl"
    jsonl.write_text("")
    totals = _sum_usage(jsonl)
    assert totals.entry_count == 0


def test_sum_usage_missing_file(tmp_path: Path):
    totals = _sum_usage(tmp_path / "nonexistent.jsonl")
    assert totals.entry_count == 0


# -- _calculate_cost tests -----------------------------------------------------


def test_calculate_cost_known_model():
    cost = _calculate_cost(
        "claude-sonnet-4-6", 1_000_000, 1_000_000, 1_000_000, 1_000_000
    )
    # 3.00 + 15.00 + 0.30 + 3.75 = 22.05
    assert cost == pytest.approx(22.05)


def test_calculate_cost_no_cache_creation():
    cost = _calculate_cost("claude-sonnet-4-6", 1_000_000, 1_000_000, 1_000_000)
    # 3.00 + 15.00 + 0.30 = 18.30 (cache_creation defaults to 0)
    assert cost == pytest.approx(18.30)


def test_calculate_cost_unknown_model():
    assert _calculate_cost("unknown-model", 100, 100, 100) is None


# -- ClaudeSessionCollector lifecycle tests ------------------------------------


def _setup_claude_dir(tmp_path: Path, cwd: str = "/my/project") -> tuple[Path, Path]:
    """Create a mock ~/.claude structure, return (claude_dir, jsonl_path)."""
    claude_dir = tmp_path / ".claude"
    _write_session(claude_dir / "sessions", 42, "test-session", cwd)

    project_name = cwd.replace("/", "-")
    jsonl_path = claude_dir / "projects" / project_name / "test-session.jsonl"
    return claude_dir, jsonl_path


def test_claude_start_capture(tmp_path: Path):
    claude_dir, jsonl_path = _setup_claude_dir(tmp_path)
    _write_jsonl(jsonl_path, [_assistant_entry(100, 50, 10, 5)])

    collector = ClaudeSessionCollector(claude_dir=claude_dir)
    config = AgentConfig(extra={"cwd": "/my/project"})
    collector.start_capture(config)

    assert collector._jsonl_path == jsonl_path
    assert collector._baseline.input_tokens == 100


def test_claude_snapshot_sums_usage(tmp_path: Path):
    claude_dir, jsonl_path = _setup_claude_dir(tmp_path)
    _write_jsonl(
        jsonl_path,
        [
            _assistant_entry(100, 50, 10, 5),
            _assistant_entry(200, 100, 20, 10),
        ],
    )

    collector = ClaudeSessionCollector(claude_dir=claude_dir)
    config = AgentConfig(extra={"cwd": "/my/project"})

    # Start with empty baseline (no pre-existing entries)
    _write_jsonl(jsonl_path, [])
    collector.start_capture(config)

    # Now write entries
    _write_jsonl(
        jsonl_path,
        [
            _assistant_entry(100, 50, 10, 5),
            _assistant_entry(200, 100, 20, 10),
        ],
    )

    output = collector.snapshot()
    assert output.input_tokens == 300
    assert output.output_tokens == 150
    assert output.cache_read_tokens == 30
    assert output.error is None


def test_claude_snapshot_delta(tmp_path: Path):
    """Only counts tokens since start_capture baseline."""
    claude_dir, jsonl_path = _setup_claude_dir(tmp_path)

    # Pre-existing entries before capture starts
    _write_jsonl(jsonl_path, [_assistant_entry(1000, 500, 100, 50)])

    collector = ClaudeSessionCollector(claude_dir=claude_dir)
    config = AgentConfig(extra={"cwd": "/my/project"})
    collector.start_capture(config)

    # New entry arrives after capture
    _write_jsonl(
        jsonl_path,
        [
            _assistant_entry(1000, 500, 100, 50),  # pre-existing
            _assistant_entry(200, 100, 20, 10),  # new
        ],
    )

    output = collector.snapshot()
    assert output.input_tokens == 200
    assert output.output_tokens == 100
    assert output.cache_read_tokens == 20


def test_claude_stop_capture_returns_final(tmp_path: Path):
    claude_dir, jsonl_path = _setup_claude_dir(tmp_path)
    _write_jsonl(jsonl_path, [])

    collector = ClaudeSessionCollector(claude_dir=claude_dir)
    config = AgentConfig(extra={"cwd": "/my/project"})
    collector.start_capture(config)

    _write_jsonl(jsonl_path, [_assistant_entry(100, 50, 10, 5)])
    output = collector.stop_capture()

    assert output.input_tokens == 100
    assert collector._jsonl_path is None  # state reset


def test_claude_calculates_cost(tmp_path: Path):
    claude_dir, jsonl_path = _setup_claude_dir(tmp_path)
    _write_jsonl(jsonl_path, [])

    collector = ClaudeSessionCollector(claude_dir=claude_dir)
    config = AgentConfig(model="claude-sonnet-4-6", extra={"cwd": "/my/project"})
    collector.start_capture(config)

    _write_jsonl(jsonl_path, [_assistant_entry(1_000_000, 1_000_000, 1_000_000, 0)])
    output = collector.snapshot()

    assert output.cost_usd == pytest.approx(18.30)  # cache_creation=0
    assert output.cost_model == "per_token"
    assert output.cost_source == "calculated"


def test_claude_cost_includes_cache_creation(tmp_path: Path):
    claude_dir, jsonl_path = _setup_claude_dir(tmp_path)
    _write_jsonl(jsonl_path, [])

    collector = ClaudeSessionCollector(claude_dir=claude_dir)
    config = AgentConfig(model="claude-sonnet-4-6", extra={"cwd": "/my/project"})
    collector.start_capture(config)

    _write_jsonl(
        jsonl_path, [_assistant_entry(1_000_000, 1_000_000, 1_000_000, 1_000_000)]
    )
    output = collector.snapshot()

    # 3.00 + 15.00 + 0.30 + 3.75 = 22.05
    assert output.cost_usd == pytest.approx(22.05)


def test_claude_no_session_found(tmp_path: Path):
    claude_dir = tmp_path / ".claude"
    (claude_dir / "sessions").mkdir(parents=True)

    collector = ClaudeSessionCollector(claude_dir=claude_dir)
    config = AgentConfig(extra={"cwd": "/nonexistent"})

    with pytest.raises(FileNotFoundError, match="No Claude Code session"):
        collector.start_capture(config)


def test_claude_snapshot_before_start():
    collector = ClaudeSessionCollector()
    output = collector.snapshot()
    assert output.error is not None
    assert "start_capture" in output.error


def test_claude_preflight(tmp_path: Path):
    collector = ClaudeSessionCollector(claude_dir=tmp_path / "nope")
    issues = collector.preflight(AgentConfig())
    assert len(issues) == 1
    assert "sessions" in issues[0]


# -- Stub collector tests ------------------------------------------------------


def test_copilot_session_stub_returns_error():
    collector = CopilotSessionCollector()
    assert collector.name == "copilot"
    assert len(collector.preflight(AgentConfig())) > 0
    output = collector.snapshot()
    assert output.error is not None
    assert "Copilot" in output.error


def test_codex_session_stub_returns_error():
    collector = CodexSessionCollector()
    assert collector.name == "codex"
    assert len(collector.preflight(AgentConfig())) > 0
    output = collector.snapshot()
    assert output.error is not None
    assert "Codex" in output.error


# -- Registry tests ------------------------------------------------------------


def test_registry_resolve_session():
    collector = resolve_session("claude")
    assert collector.name == "claude"


def test_registry_resolve_session_unknown():
    with pytest.raises(KeyError, match="unknown-collector"):
        resolve_session("unknown-collector")


def test_registry_available_sessions():
    names = available_sessions()
    assert "claude" in names
    assert "copilot" in names
    assert "codex" in names
