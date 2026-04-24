"""End-to-end wiring test for ``codeprobe run`` + R5 trace.db.

The TraceRecorder/store infra is unit-tested in ``tests/trace/``; this
module asserts that the CLI pipeline actually instantiates a recorder,
threads it through the executor, and writes ``runs/trace.db`` with real
rows when a task completes. Without this test the recorder silently
orphans and callers see an empty DB (the defect this bead fixes).
"""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.adapters.protocol import AgentConfig, AgentOutput
from codeprobe.adapters.telemetry import JsonStdoutCollector
from codeprobe.cli import main

# A valid Claude CLI ``--output-format stream-json --verbose`` transcript.
# One assistant event with two ``tool_use`` blocks, followed by a
# terminal ``result`` event carrying usage + cost. The recorder's
# ingest_stream() turns this into three rows (2 tool_use + 1 result).
_STREAM_JSON_TRANSCRIPT = "\n".join(
    [
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"path": "main.py"},
                        },
                        {
                            "type": "tool_use",
                            "name": "Bash",
                            "input": {"command": "pytest"},
                        },
                    ]
                },
            }
        ),
        json.dumps(
            {
                "type": "result",
                "result": "Done.",
                "usage": {
                    "input_tokens": 42,
                    "output_tokens": 7,
                },
                "total_cost_usd": 0.001,
            }
        ),
    ]
)


class _StreamJsonFakeClaudeAdapter:
    """FakeAdapter that mimics the Claude adapter's trace-context contract.

    Shape required by the wiring:

    1. ``run()`` returns an ``AgentOutput`` whose ``stdout`` is a valid
       ``--output-format stream-json`` transcript.
    2. Exposes ``set_trace_context(recorder, config, task_id)`` — the
       executor sets this per-task. On ``run()`` we call the real
       ``JsonStdoutCollector`` with the trace context so rows actually
       land in ``trace.db``. This mirrors ``ClaudeAdapter.parse_output``
       without rebuilding the whole adapter.
    """

    def __init__(self) -> None:
        self._collector = JsonStdoutCollector()
        self._trace_recorder: object | None = None
        self._trace_config: str | None = None
        self._trace_task_id: str | None = None

    @property
    def name(self) -> str:
        return "fake-claude"

    def find_binary(self) -> str | None:
        return "/usr/bin/true"

    def preflight(self, config: AgentConfig) -> list[str]:
        return []

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        return ["true"]

    def isolate_session(self, slot_id: int) -> dict[str, str]:
        return {}

    def set_trace_context(
        self,
        *,
        recorder: object | None,
        config: str | None,
        task_id: str | None,
    ) -> None:
        self._trace_recorder = recorder
        self._trace_config = config
        self._trace_task_id = task_id

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
        stdout = _STREAM_JSON_TRANSCRIPT
        # Mirror ClaudeAdapter.parse_output: forward trace context so the
        # JsonStdoutCollector's hook ingests the stream into trace.db.
        ctx: dict[str, object] = {}
        if (
            self._trace_recorder is not None
            and self._trace_config is not None
            and self._trace_task_id is not None
        ):
            ctx = {
                "trace_recorder": self._trace_recorder,
                "trace_config": self._trace_config,
                "trace_task_id": self._trace_task_id,
            }
        usage = self._collector.collect(stdout, **ctx)
        return AgentOutput(
            stdout="Done.",
            stderr=None,
            exit_code=0,
            duration_seconds=0.1,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost_usd,
            cost_model=usage.cost_model,
            cost_source=usage.cost_source,
            tool_call_count=usage.tool_call_count,
            tool_use_by_name=usage.tool_use_by_name,
        )


def _setup_experiment(tmp_path: Path) -> Path:
    """Create a minimal experiment directory with a single passing task."""
    exp_dir = tmp_path / ".codeprobe" / "trace-test"
    task_dir = exp_dir / "tasks" / "task-001"
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True)

    (task_dir / "instruction.md").write_text("Trivial task.\n", encoding="utf-8")
    test_sh = tests_dir / "test.sh"
    test_sh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)

    experiment = {
        "name": "trace-test",
        "description": "R5 wiring e2e",
        "tasks_dir": "tasks",
        "configs": [
            {
                "label": "baseline",
                "agent": "fake",
                "permission_mode": "default",
            }
        ],
        "task_ids": ["task-001"],
    }
    (exp_dir / "experiment.json").write_text(
        json.dumps(experiment), encoding="utf-8"
    )
    return exp_dir


def test_run_emits_trace_db_with_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``codeprobe run`` writes ``runs/trace.db`` populated with events.

    This is the AC1 acceptance gate for r5-trace-sqlite: the recorder
    must actually be wired into the run loop — not just exist as an
    independently-tested module.
    """
    exp_dir = _setup_experiment(tmp_path)

    # Stub the adapter resolver so "claude" / "fake" both return our stub.
    # run_cmd.py calls resolve(...) at two points: once to validate the
    # agent name, and once per-config when picking the adapter. A single
    # monkeypatch handles both.
    adapter = _StreamJsonFakeClaudeAdapter()
    monkeypatch.setattr(
        "codeprobe.cli.run_cmd.resolve",
        lambda _name: adapter,
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "run",
            str(exp_dir),
            "--agent",
            "fake",
            "--parallel",
            "1",
            "--force-plain",
        ],
        catch_exceptions=False,
    )

    assert result.exit_code == 0, (
        f"codeprobe run failed (exit={result.exit_code}). Output:\n{result.output}"
    )

    trace_db = exp_dir / "runs" / "trace.db"
    assert trace_db.is_file(), (
        f"runs/trace.db was NOT created at {trace_db}. "
        f"AC1 failing — TraceRecorder is not wired into the run loop.\n"
        f"CLI output:\n{result.output}"
    )

    # Assert the DB schema is intact and has >=1 event for our task.
    with sqlite3.connect(trace_db) as conn:
        cur = conn.execute("SELECT COUNT(*) FROM events")
        (count,) = cur.fetchone()
        assert count >= 1, (
            f"trace.db exists but holds {count} events. "
            "Expected the ingest_stream hook to record tool_use + result rows."
        )

        cur = conn.execute(
            "SELECT config, task_id, event_type FROM events "
            "WHERE task_id = ? ORDER BY event_seq",
            ("task-001",),
        )
        rows = cur.fetchall()
        assert rows, "no rows for task-001 — context threading is broken"
        configs = {r[0] for r in rows}
        assert configs == {"baseline"}, (
            f"expected all rows tagged config=baseline, got {configs}"
        )
        event_types = {r[2] for r in rows}
        # The stream-json transcript has 2 tool_use blocks + 1 result event.
        assert "tool_use" in event_types, (
            f"expected tool_use rows; got event_types={event_types}"
        )


def test_run_rejects_bogus_trace_overflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7 — ``--trace-overflow`` only accepts 'fail' or 'truncate'."""
    exp_dir = _setup_experiment(tmp_path)

    monkeypatch.setattr(
        "codeprobe.cli.run_cmd.resolve",
        lambda _name: _StreamJsonFakeClaudeAdapter(),
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "run",
            str(exp_dir),
            "--agent",
            "fake",
            "--trace-overflow",
            "nope",
        ],
    )
    # Click's Choice type rejects the value before run_eval is called.
    assert result.exit_code != 0
    assert "nope" in result.output or "invalid choice" in result.output.lower()


def test_run_accepts_trace_deny_glob(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC7 — ``--trace-deny`` is accepted and repeatable without crashing."""
    exp_dir = _setup_experiment(tmp_path)

    monkeypatch.setattr(
        "codeprobe.cli.run_cmd.resolve",
        lambda _name: _StreamJsonFakeClaudeAdapter(),
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "run",
            str(exp_dir),
            "--agent",
            "fake",
            "--parallel",
            "1",
            "--force-plain",
            "--trace-deny",
            "*secret*",
            "--trace-deny",
            "*password*",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    assert (exp_dir / "runs" / "trace.db").is_file()
