"""SessionCollector protocol — observe interactive agent sessions."""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast, runtime_checkable

from codeprobe.adapters.protocol import AgentConfig, AgentOutput
from codeprobe.adapters.telemetry import CLAUDE_PRICING

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _TokenTotals:
    """Accumulated token counts from a session JSONL scan."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    entry_count: int = 0


@runtime_checkable
class SessionCollector(Protocol):
    """Protocol for observing interactive agent sessions.

    Unlike ``AgentAdapter.run(prompt)``, a ``SessionCollector`` does not
    execute an agent — it observes an already-running session.

    Lifecycle: ``start_capture`` → ``snapshot`` (repeatable) → ``stop_capture``.
    """

    @property
    def name(self) -> str: ...

    def preflight(self, config: AgentConfig) -> list[str]: ...

    def start_capture(self, config: AgentConfig) -> None: ...

    def snapshot(self) -> AgentOutput:
        """Return current session telemetry since ``start_capture``."""
        ...

    def stop_capture(self) -> AgentOutput:
        """Return final telemetry and release resources."""
        ...


# ---------------------------------------------------------------------------
#  Claude Code session collector
# ---------------------------------------------------------------------------

_CLAUDE_DIR = Path.home() / ".claude"


def _find_session(
    sessions_dir: Path, cwd: str, session_id: str | None = None
) -> dict | None:
    """Find a session JSON matching the given cwd (or explicit session_id)."""
    if not sessions_dir.is_dir():
        return None

    best: dict | None = None
    for path in sessions_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        if session_id and data.get("sessionId") == session_id:
            return cast("dict[Any, Any]", data)

        if data.get("cwd") == cwd:
            if best is None or data.get("startedAt", 0) > best.get("startedAt", 0):
                best = data

    return best


def _sum_usage(jsonl_path: Path) -> _TokenTotals:
    """Sum token usage from all assistant entries in a session JSONL file."""
    input_tokens = 0
    output_tokens = 0
    cache_read = 0
    cache_creation = 0
    count = 0

    if not jsonl_path.is_file():
        return _TokenTotals()

    with jsonl_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("type") != "assistant":
                continue

            usage = entry.get("message", {}).get("usage")
            if usage is None:
                continue

            input_tokens += usage.get("input_tokens", 0)
            output_tokens += usage.get("output_tokens", 0)
            cache_read += usage.get("cache_read_input_tokens", 0)
            cache_creation += usage.get("cache_creation_input_tokens", 0)
            count += 1

    return _TokenTotals(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read,
        cache_creation_tokens=cache_creation,
        entry_count=count,
    )


def _project_dir_name(cwd: str) -> str:
    """Convert a cwd path to the Claude project directory name.

    Claude Code encodes project directories by replacing ``/`` with ``-``.
    E.g., ``/home/ds/codeprobe`` → ``-home-ds-codeprobe``.
    """
    return cwd.replace("/", "-")


def _calculate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int = 0,
) -> float | None:
    """Calculate USD cost from token counts and Claude pricing table."""
    pricing = CLAUDE_PRICING.get(model)
    if pricing is None:
        return None
    input_rate, output_rate, cache_read_rate, cache_write_rate = pricing
    return (
        input_tokens * input_rate / 1_000_000
        + output_tokens * output_rate / 1_000_000
        + cache_read_tokens * cache_read_rate / 1_000_000
        + cache_creation_tokens * cache_write_rate / 1_000_000
    )


class ClaudeSessionCollector:
    """Observe an active Claude Code session via ~/.claude/ JSONL files."""

    def __init__(self, claude_dir: Path | None = None) -> None:
        self._claude_dir = claude_dir or _CLAUDE_DIR
        self._jsonl_path: Path | None = None
        self._baseline: _TokenTotals = _TokenTotals()
        self._start_time: float = 0.0
        self._model: str = ""

    @property
    def name(self) -> str:
        return "claude"

    def preflight(self, config: AgentConfig) -> list[str]:
        issues: list[str] = []
        sessions_dir = self._claude_dir / "sessions"
        if not sessions_dir.is_dir():
            issues.append(f"Claude sessions directory not found: {sessions_dir}")
        return issues

    def start_capture(self, config: AgentConfig) -> None:
        """Locate the active session JSONL and record baseline token counts."""
        extra = config.extra or {}
        session_id: str | None = extra.get("session_id")
        cwd: str = extra.get("cwd", str(Path.cwd()))

        sessions_dir = self._claude_dir / "sessions"
        session = _find_session(sessions_dir, cwd, session_id)

        if session is None:
            raise FileNotFoundError(f"No Claude Code session found for cwd={cwd!r}")

        sid = session.get("sessionId")
        if not sid:
            raise ValueError(f"Session file missing 'sessionId': {session}")

        project_name = _project_dir_name(session.get("cwd", cwd))
        jsonl_path = (
            self._claude_dir / "projects" / project_name / f"{sid}.jsonl"
        ).resolve()
        allowed_root = (self._claude_dir / "projects").resolve()
        if not str(jsonl_path).startswith(str(allowed_root) + "/"):
            raise ValueError(f"Session path escapes claude dir: {jsonl_path}")
        self._jsonl_path = jsonl_path

        self._model = config.model or "claude-sonnet-4-6"
        self._baseline = _sum_usage(self._jsonl_path)
        self._start_time = time.monotonic()

    def snapshot(self) -> AgentOutput:
        """Return token/cost deltas since start_capture."""
        if self._jsonl_path is None:
            return AgentOutput(
                stdout="",
                stderr=None,
                exit_code=0,
                duration_seconds=0.0,
                error="start_capture() has not been called",
            )

        current = _sum_usage(self._jsonl_path)
        duration = time.monotonic() - self._start_time

        input_delta = current.input_tokens - self._baseline.input_tokens
        output_delta = current.output_tokens - self._baseline.output_tokens
        cache_read_delta = current.cache_read_tokens - self._baseline.cache_read_tokens
        cache_creation_delta = (
            current.cache_creation_tokens - self._baseline.cache_creation_tokens
        )
        turns = current.entry_count - self._baseline.entry_count

        cost = _calculate_cost(
            self._model,
            input_delta,
            output_delta,
            cache_read_delta,
            cache_creation_delta,
        )
        cost_model = "per_token" if cost is not None else "unknown"
        cost_source = "calculated" if cost is not None else "unavailable"

        return AgentOutput(
            stdout=f"Session snapshot: {turns} turns since capture start",
            stderr=None,
            exit_code=0,
            duration_seconds=duration,
            input_tokens=input_delta,
            output_tokens=output_delta,
            cache_read_tokens=cache_read_delta,
            cost_usd=cost,
            cost_model=cost_model,
            cost_source=cost_source,
        )

    def stop_capture(self) -> AgentOutput:
        """Return final snapshot and reset state."""
        result = self.snapshot()
        self._jsonl_path = None
        self._baseline = _TokenTotals()
        self._start_time = 0.0
        return result


# ---------------------------------------------------------------------------
#  Stub collectors
# ---------------------------------------------------------------------------

_UNSUPPORTED_MSG = "Session telemetry not yet supported for {agent}"


class _StubSessionCollector:
    """Base stub for agents whose session telemetry is not yet implemented."""

    _agent_name: str = ""
    _agent_label: str = ""

    @property
    def name(self) -> str:
        return self._agent_name

    def preflight(self, config: AgentConfig) -> list[str]:
        return [_UNSUPPORTED_MSG.format(agent=self._agent_label)]

    def start_capture(self, config: AgentConfig) -> None:
        pass

    def snapshot(self) -> AgentOutput:
        return AgentOutput(
            stdout="",
            stderr=None,
            exit_code=0,
            duration_seconds=0.0,
            error=_UNSUPPORTED_MSG.format(agent=self._agent_label),
        )

    def stop_capture(self) -> AgentOutput:
        return self.snapshot()


class CopilotSessionCollector(_StubSessionCollector):
    _agent_name = "copilot"
    _agent_label = "Copilot"


class CodexSessionCollector(_StubSessionCollector):
    _agent_name = "codex"
    _agent_label = "Codex"
