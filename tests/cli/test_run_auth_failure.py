"""Run CLI regressions for adapter authentication failures."""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path

import pytest
from rich.console import Console

from codeprobe.adapters.protocol import AgentConfig
from codeprobe.cli.rich_display import RichLiveListener
from codeprobe.cli.run_cmd import NdjsonStdoutListener, PlainTextListener
from codeprobe.core.events import EventDispatcher, RunStarted, TaskScored
from codeprobe.core.executor import execute_config
from codeprobe.models.experiment import ExperimentConfig
from tests.conftest import FakeAdapter

pytestmark = pytest.mark.usefixtures("fake_worktree_isolation")


def _make_task(task_dir: Path) -> Path:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text("Fix the bug.")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    test_sh = tests_dir / "test.sh"
    test_sh.write_text("#!/bin/bash\nexit 0\n")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)
    return task_dir


def _auth_task_scored() -> TaskScored:
    return TaskScored(
        task_id="auth-broken",
        config_label="candidate",
        automated_score=0.0,
        duration_seconds=0.2,
        cost_usd=None,
        input_tokens=None,
        output_tokens=None,
        cache_read_tokens=None,
        cache_creation_tokens=None,
        cost_model="unknown",
        cost_source="unavailable",
        error="Authentication failed: invalid API key",
        timestamp=0.0,
        status="error",
        error_category="auth_failure",
    )


def test_plain_text_classifies_auth_error_without_score(
    capsys: pytest.CaptureFixture[str],
) -> None:
    PlainTextListener().on_event(_auth_task_scored())

    output = capsys.readouterr().out
    assert "auth-broken: AUTH_ERROR" in output
    assert "FAIL" not in output
    assert "0.00" not in output


def test_ndjson_event_classifies_adapter_auth_error_without_score(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tasks = [_make_task(tmp_path / "task-001")]
    adapter = FakeAdapter(
        stdout="provider emitted an auth envelope",
        error="Authentication failed: invalid API key",
        error_category="auth_failure",
    )
    dispatcher = EventDispatcher()
    dispatcher.register(NdjsonStdoutListener())

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=ExperimentConfig(label="candidate"),
        agent_config=AgentConfig(),
        event_dispatcher=dispatcher,
    )
    dispatcher.shutdown()

    assert len(results) == 1
    assert results[0].status == "error"
    assert results[0].error_category == "auth_failure"
    assert "remediation" in results[0].metadata

    [payload] = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert payload["event"] == "task_done"
    assert payload["task_id"] == "task-001"
    assert payload["outcome"] == "auth_failure"
    assert payload["error_category"] == "auth_failure"
    assert "score" not in payload
    assert "invalid API key" in payload["error"]


def test_rich_listener_renders_auth_error_without_scoring_it() -> None:
    listener = RichLiveListener()
    listener._console = Console(  # type: ignore[attr-defined]
        file=io.StringIO(),
        force_terminal=False,
        width=200,
        color_system=None,
    )
    listener.on_event(
        RunStarted(total_tasks=1, config_label="candidate", timestamp=0.0)
    )
    try:
        listener.on_event(_auth_task_scored())
        rendered_buffer = io.StringIO()
        Console(
            file=rendered_buffer,
            force_terminal=False,
            width=200,
            color_system=None,
        ).print(listener._build_display())  # type: ignore[attr-defined]
    finally:
        if listener._live is not None:  # type: ignore[attr-defined]
            listener._live.stop()  # type: ignore[attr-defined]

    state = listener._configs["candidate"]  # type: ignore[attr-defined]
    rendered = rendered_buffer.getvalue()
    assert "AUTH_ERROR" in rendered
    assert "0 scored, 1 infra" in rendered
    assert state.scored_count == 0
    assert state.infra_failure_count == 1
    assert state.passed == 0


def test_execute_config_sequential_halts_on_auth_failure(tmp_path: Path) -> None:
    tasks = [_make_task(tmp_path / f"task-{i:03d}") for i in range(4)]
    adapter = FakeAdapter(
        stdout="provider emitted an auth envelope",
        error="Authentication failed: expired token",
        error_category="auth_failure",
    )

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=ExperimentConfig(label="candidate"),
        agent_config=AgentConfig(),
        parallel=1,
    )

    assert len(results) == 1
    assert len(adapter.run_calls) == 1
    assert results[0].status == "error"
    assert results[0].error_category == "auth_failure"
    assert "expired token" in results[0].metadata["error"]
    assert "Refresh authentication" in results[0].metadata["remediation"]
