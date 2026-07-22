"""Tests for event dispatcher integration with executor."""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest

from codeprobe.adapters.protocol import AgentConfig
from codeprobe.core.events import (
    BudgetWarning,
    EventDispatcher,
    RunEvent,
    RunFinished,
    RunStarted,
    TaskScored,
    TaskStarted,
)
from codeprobe.core.executor import execute_config
from codeprobe.models.experiment import CompletedTask, ExperimentConfig
from tests.conftest import FakeAdapter, SequentialCostAdapter

# These tests run execute_config against a nonexistent repo path; every run
# path now requires a worktree slot (codeprobe-f7rl.2), so use the
# passthrough isolation fake from tests/conftest.py.
pytestmark = pytest.mark.usefixtures("fake_worktree_isolation")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task_dir(base: Path, name: str, *, passing: bool = True) -> Path:
    """Create a minimal task directory with instruction and test.sh."""
    task_dir = base / name
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text("Fix the bug.")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    test_sh = tests_dir / "test.sh"
    exit_code = 0 if passing else 1
    test_sh.write_text(f"#!/bin/bash\nexit {exit_code}\n")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)
    return task_dir


def _write_metadata(
    task_dir: Path,
    *,
    category: str | None = None,
    verification_type: str | None = None,
    max_turns_override: int | None = None,
) -> None:
    """Write a metadata.json with the fields the turn-cap resolver reads."""
    meta_block: dict = {"name": task_dir.name}
    if category is not None:
        meta_block["category"] = category
    if max_turns_override is not None:
        meta_block["max_turns_override"] = max_turns_override
    verification: dict = {}
    if verification_type is not None:
        verification["type"] = verification_type
    payload = {"id": task_dir.name, "metadata": meta_block, "verification": verification}
    (task_dir / "metadata.json").write_text(json.dumps(payload), encoding="utf-8")


class RecordingListener:
    """Listener that records all events in order."""

    def __init__(self) -> None:
        self.events: list[RunEvent] = []

    def on_event(self, event: RunEvent) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Event order tests
# ---------------------------------------------------------------------------


class TestEventOrder:
    """execute_config with EventDispatcher emits events in correct order."""

    def test_three_task_run_emits_correct_event_sequence(self, tmp_path: Path) -> None:
        """For a 3-task run: RunStarted, 3x(TaskStarted, TaskScored), RunFinished."""
        tasks = [_make_task_dir(tmp_path, f"task-{i:03d}") for i in range(3)]
        adapter = FakeAdapter(stdout="output", cost_usd=0.05, cost_model="per_token")
        exp_config = ExperimentConfig(label="test-events")
        agent_config = AgentConfig()

        dispatcher = EventDispatcher()
        recorder = RecordingListener()
        dispatcher.register(recorder)

        try:
            results = execute_config(
                adapter=adapter,
                task_dirs=tasks,
                repo_path=Path("/repo"),
                experiment_config=exp_config,
                agent_config=agent_config,
                event_dispatcher=dispatcher,
            )
        finally:
            dispatcher.shutdown()

        assert len(results) == 3

        events = recorder.events
        # Filter to lifecycle events (exclude BudgetWarning which may or may not appear)
        lifecycle = [
            e
            for e in events
            if isinstance(e, (RunStarted, TaskStarted, TaskScored, RunFinished))
        ]

        # First event: RunStarted
        assert isinstance(lifecycle[0], RunStarted)
        assert lifecycle[0].total_tasks == 3
        assert lifecycle[0].config_label == "test-events"

        # Then alternating TaskStarted, TaskScored
        for i in range(3):
            idx = 1 + i * 2
            assert isinstance(
                lifecycle[idx], TaskStarted
            ), f"Expected TaskStarted at position {idx}, got {type(lifecycle[idx]).__name__}"
            assert isinstance(
                lifecycle[idx + 1], TaskScored
            ), f"Expected TaskScored at position {idx + 1}, got {type(lifecycle[idx + 1]).__name__}"

        # Last event: RunFinished
        assert isinstance(lifecycle[-1], RunFinished)
        assert lifecycle[-1].completed_count == 3
        assert lifecycle[-1].total_tasks == 3

    def test_task_scored_has_correct_fields(self, tmp_path: Path) -> None:
        """TaskScored events carry the right data from CompletedTask."""
        tasks = [_make_task_dir(tmp_path, "task-fields")]
        adapter = FakeAdapter(
            stdout="output",
            cost_usd=0.07,
            cost_model="per_token",
            duration=2.5,
        )
        exp_config = ExperimentConfig(label="field-check")
        agent_config = AgentConfig()

        dispatcher = EventDispatcher()
        recorder = RecordingListener()
        dispatcher.register(recorder)

        try:
            execute_config(
                adapter=adapter,
                task_dirs=tasks,
                repo_path=Path("/repo"),
                experiment_config=exp_config,
                agent_config=agent_config,
                event_dispatcher=dispatcher,
            )
        finally:
            dispatcher.shutdown()

        scored = [e for e in recorder.events if isinstance(e, TaskScored)]
        assert len(scored) == 1
        ev = scored[0]
        assert ev.task_id == "task-fields"
        assert ev.config_label == "field-check"
        assert ev.cost_usd == 0.07
        assert ev.cost_model == "per_token"
        assert ev.duration_seconds == 2.5

    def test_run_finished_summary_stats(self, tmp_path: Path) -> None:
        """RunFinished contains correct summary statistics."""
        tasks = [_make_task_dir(tmp_path, f"task-{i:03d}") for i in range(2)]
        adapter = FakeAdapter(
            stdout="output", cost_usd=0.10, cost_model="per_token", duration=3.0
        )
        exp_config = ExperimentConfig(label="summary")
        agent_config = AgentConfig()

        dispatcher = EventDispatcher()
        recorder = RecordingListener()
        dispatcher.register(recorder)

        try:
            execute_config(
                adapter=adapter,
                task_dirs=tasks,
                repo_path=Path("/repo"),
                experiment_config=exp_config,
                agent_config=agent_config,
                event_dispatcher=dispatcher,
            )
        finally:
            dispatcher.shutdown()

        finished = [e for e in recorder.events if isinstance(e, RunFinished)]
        assert len(finished) == 1
        rf = finished[0]
        assert rf.completed_count == 2
        assert rf.total_cost == pytest.approx(0.20, abs=0.01)
        assert rf.total_duration == pytest.approx(6.0, abs=0.5)


# ---------------------------------------------------------------------------
# BudgetChecker integration
# ---------------------------------------------------------------------------


class TestBudgetCheckerIntegration:
    """BudgetChecker registered via dispatcher halts execution and emits warnings."""

    def test_budget_exceeded_halts_execution(self, tmp_path: Path) -> None:
        """When cost exceeds budget, remaining tasks are skipped."""
        tasks = [_make_task_dir(tmp_path, f"task-{i:03d}") for i in range(5)]
        # Each task costs $0.05; budget is $0.10 so after 2 tasks we're at $0.10
        adapter = FakeAdapter(stdout="output", cost_usd=0.05, cost_model="per_token")
        exp_config = ExperimentConfig(label="budget-halt")
        agent_config = AgentConfig()

        dispatcher = EventDispatcher()
        recorder = RecordingListener()
        dispatcher.register(recorder)

        try:
            results = execute_config(
                adapter=adapter,
                task_dirs=tasks,
                repo_path=Path("/repo"),
                experiment_config=exp_config,
                agent_config=agent_config,
                max_cost_usd=0.10,
                event_dispatcher=dispatcher,
            )
        finally:
            dispatcher.shutdown()

        # Should have fewer than 5 results due to budget halt
        assert len(results) < 5
        # BudgetWarning should have been emitted (80% at $0.08, exceeded at $0.10)
        warnings = [e for e in recorder.events if isinstance(e, BudgetWarning)]
        assert len(warnings) >= 1

    def test_budget_warning_emitted_at_threshold(self, tmp_path: Path) -> None:
        """BudgetWarning is emitted when cumulative cost crosses 80% threshold."""
        tasks = [_make_task_dir(tmp_path, f"task-{i:03d}") for i in range(5)]
        # Costs: $0.02, $0.02, $0.06, $0.02, $0.02 — budget $0.12, 80% = $0.096
        # After task 3: cumulative = $0.10 (83%) triggers warning
        adapter = SequentialCostAdapter(
            costs=[
                (0.02, "per_token"),
                (0.02, "per_token"),
                (0.06, "per_token"),
                (0.02, "per_token"),
                (0.02, "per_token"),
            ],
            stdout="output",
        )
        exp_config = ExperimentConfig(label="budget-warn")
        agent_config = AgentConfig()

        dispatcher = EventDispatcher()
        recorder = RecordingListener()
        dispatcher.register(recorder)

        try:
            execute_config(
                adapter=adapter,
                task_dirs=tasks,
                repo_path=Path("/repo"),
                experiment_config=exp_config,
                agent_config=agent_config,
                max_cost_usd=0.12,
                event_dispatcher=dispatcher,
            )
        finally:
            dispatcher.shutdown()

        warnings = [e for e in recorder.events if isinstance(e, BudgetWarning)]
        assert len(warnings) >= 1
        # First warning should be at 80% threshold
        assert warnings[0].threshold_pct == pytest.approx(0.8)

    def test_shared_checker_warns_once_per_experiment(self, tmp_path: Path) -> None:
        """With one checker shared across two arms, the 80% and 100%
        warnings fire once for the EXPERIMENT, not once per arm
        (codeprobe-f7rl.33)."""
        from codeprobe.core.events import BudgetChecker

        tasks_a = [_make_task_dir(tmp_path / "a", f"task-{i:03d}") for i in range(2)]
        tasks_b = [_make_task_dir(tmp_path / "b", f"task-{i:03d}") for i in range(2)]
        checker = BudgetChecker(budget=1.0)
        agent_config = AgentConfig()

        recorders: list[RecordingListener] = []
        for label, tasks in (("arm-a", tasks_a), ("arm-b", tasks_b)):
            adapter = FakeAdapter(
                stdout="output", cost_usd=0.3, cost_model="per_token"
            )
            dispatcher = EventDispatcher()
            recorder = RecordingListener()
            dispatcher.register(recorder)
            try:
                execute_config(
                    adapter=adapter,
                    task_dirs=tasks,
                    repo_path=Path("/repo"),
                    experiment_config=ExperimentConfig(label=label),
                    agent_config=agent_config,
                    max_cost_usd=1.0,
                    event_dispatcher=dispatcher,
                    budget_checker=checker,
                )
            finally:
                dispatcher.shutdown()
            recorders.append(recorder)

        # Arm A spends $0.60 (no threshold crossed). Arm B crosses 80% at
        # $0.90 and 100% at $1.20 — exactly one warning each, experiment-wide.
        warnings = [
            e
            for r in recorders
            for e in r.events
            if isinstance(e, BudgetWarning)
        ]
        assert sorted(w.threshold_pct for w in warnings) == [
            pytest.approx(0.8),
            pytest.approx(1.0),
        ]
        assert checker.is_exceeded
        assert checker.cumulative_cost == pytest.approx(1.2)


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompat:
    """execute_config with on_task_complete (no dispatcher) still works."""

    def test_on_task_complete_called_without_dispatcher(self, tmp_path: Path) -> None:
        """Legacy on_task_complete callback fires when no dispatcher is provided."""
        tasks = [_make_task_dir(tmp_path, f"task-{i:03d}") for i in range(3)]
        adapter = FakeAdapter(stdout="output", cost_usd=0.05, cost_model="per_token")
        exp_config = ExperimentConfig(label="compat")
        agent_config = AgentConfig()

        callback_results: list[CompletedTask] = []

        def _callback(result: CompletedTask) -> None:
            callback_results.append(result)

        results = execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            on_task_complete=_callback,
        )

        assert len(results) == 3
        assert len(callback_results) == 3
        assert all(r.task_id.startswith("task-") for r in callback_results)

    def test_legacy_budget_warning_without_dispatcher(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without dispatcher, inline 80% budget warning still prints to stderr."""
        tasks = [_make_task_dir(tmp_path, f"task-{i:03d}") for i in range(5)]
        adapter = SequentialCostAdapter(
            costs=[
                (0.02, "per_token"),
                (0.02, "per_token"),
                (0.06, "per_token"),
                (0.02, "per_token"),
                (0.02, "per_token"),
            ],
            stdout="output",
        )
        exp_config = ExperimentConfig(label="legacy-budget")
        agent_config = AgentConfig()

        execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            max_cost_usd=0.12,
            # No event_dispatcher — legacy path
        )
        captured = capsys.readouterr()
        assert "Cost warning:" in captured.err
        assert "budget used" in captured.err


# ---------------------------------------------------------------------------
# PlainTextListener output
# ---------------------------------------------------------------------------


class TestPlainTextListener:
    """PlainTextListener produces expected output on stderr/stdout."""

    def test_task_scored_prints_pass_fail(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """TaskScored events produce PASS/FAIL lines on stdout."""
        from codeprobe.cli.run_cmd import PlainTextListener

        listener = PlainTextListener()
        listener.on_event(
            TaskScored(
                task_id="task-001",
                config_label="test",
                automated_score=1.0,
                duration_seconds=2.5,
                cost_usd=0.05,
                input_tokens=100,
                output_tokens=50,
                cache_read_tokens=None,
                cache_creation_tokens=None,
                cost_model="per_token",
                cost_source="api",
                error=None,
                timestamp=time.time(),
            )
        )
        listener.on_event(
            TaskScored(
                task_id="task-002",
                config_label="test",
                automated_score=0.0,
                duration_seconds=1.0,
                cost_usd=0.03,
                input_tokens=80,
                output_tokens=40,
                cache_read_tokens=None,
                cache_creation_tokens=None,
                cost_model="per_token",
                cost_source="api",
                error="test failed",
                timestamp=time.time(),
            )
        )
        captured = capsys.readouterr()
        assert "task-001: PASS" in captured.out
        assert "task-002: FAIL" in captured.out

    def test_budget_warning_prints_to_stderr(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """BudgetWarning events print to stderr."""
        from codeprobe.cli.run_cmd import PlainTextListener

        listener = PlainTextListener()
        listener.on_event(
            BudgetWarning(
                cumulative_cost=0.85,
                budget=1.00,
                threshold_pct=0.8,
                timestamp=time.time(),
            )
        )
        captured = capsys.readouterr()
        assert "Cost warning:" in captured.err
        assert "$0.85" in captured.err
        assert "$1.00" in captured.err

    def test_run_finished_prints_summary(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """RunFinished events print a summary line."""
        from codeprobe.cli.run_cmd import PlainTextListener

        listener = PlainTextListener()
        listener.on_event(
            RunFinished(
                total_tasks=5,
                completed_count=5,
                mean_score=0.80,
                total_cost=0.50,
                total_duration=15.0,
                config_label="test",
                timestamp=time.time(),
            )
        )
        captured = capsys.readouterr()
        assert "Finished: 5/5 tasks" in captured.out
        assert "mean score 0.80" in captured.out
        assert "$0.50" in captured.out


# ---------------------------------------------------------------------------
# Task-category-aware turn cap (codeprobe-gg9f)
# ---------------------------------------------------------------------------


class TestTurnCapResolution:
    """execute_config resolves the per-trial turn cap and surfaces it.

    Asserts both the cap the adapter actually receives (after the resolver
    injects it into AgentConfig) and the ``max_turns_chosen`` /
    ``max_turns_source`` envelope fields on the CompletedTask.
    """

    def _run(
        self,
        task_dir: Path,
        *,
        agent_config: AgentConfig,
        config_max_turns_source: str = "",
    ) -> tuple[FakeAdapter, CompletedTask]:
        adapter = FakeAdapter(stdout="output", cost_usd=0.01, cost_model="per_token")
        results = execute_config(
            adapter=adapter,
            task_dirs=[task_dir],
            repo_path=Path("/repo"),
            experiment_config=ExperimentConfig(label="turncap"),
            agent_config=agent_config,
            config_max_turns_source=config_max_turns_source,
        )
        assert len(results) == 1
        return adapter, results[0]

    def test_sdlc_without_override_is_uncapped(self, tmp_path: Path) -> None:
        task = _make_task_dir(tmp_path, "sdlc-task")
        _write_metadata(task, category="sdlc")

        adapter, result = self._run(task, agent_config=AgentConfig())

        assert adapter.run_calls[0][1].max_turns is None
        assert result.metadata["max_turns_chosen"] is None
        assert result.metadata["max_turns_source"] == "family_default"

    def test_oracle_checks_gets_family_default_50(self, tmp_path: Path) -> None:
        task = _make_task_dir(tmp_path, "oracle-task")
        _write_metadata(task, verification_type="oracle_checks")

        adapter, result = self._run(task, agent_config=AgentConfig())

        assert adapter.run_calls[0][1].max_turns == 50
        assert result.metadata["max_turns_chosen"] == 50
        assert result.metadata["max_turns_source"] == "family_default"

    def test_unknown_family_gets_default_fallback_75(self, tmp_path: Path) -> None:
        task = _make_task_dir(tmp_path, "micro-task")
        _write_metadata(task, category="micro_probe")

        adapter, result = self._run(task, agent_config=AgentConfig())

        assert adapter.run_calls[0][1].max_turns == 75
        assert result.metadata["max_turns_source"] == "family_default"

    def test_task_override_beats_family_default(self, tmp_path: Path) -> None:
        task = _make_task_dir(tmp_path, "override-task")
        _write_metadata(task, category="sdlc", max_turns_override=30)

        adapter, result = self._run(task, agent_config=AgentConfig())

        assert adapter.run_calls[0][1].max_turns == 30
        assert result.metadata["max_turns_chosen"] == 30
        assert result.metadata["max_turns_source"] == "task"

    def test_cli_cap_beats_task_override_and_family(self, tmp_path: Path) -> None:
        task = _make_task_dir(tmp_path, "cli-task")
        _write_metadata(task, category="sdlc", max_turns_override=30)

        adapter, result = self._run(
            task,
            agent_config=AgentConfig(max_turns=42),
            config_max_turns_source="cli",
        )

        assert adapter.run_calls[0][1].max_turns == 42
        assert result.metadata["max_turns_chosen"] == 42
        assert result.metadata["max_turns_source"] == "cli"

    def test_invalid_override_falls_through_to_family(self, tmp_path: Path) -> None:
        # A non-positive / malformed override is ignored at runtime; the
        # family default decides instead.
        task = _make_task_dir(tmp_path, "bad-override")
        _write_metadata(task, verification_type="oracle_checks", max_turns_override=0)

        adapter, result = self._run(task, agent_config=AgentConfig())

        assert adapter.run_calls[0][1].max_turns == 50
        assert result.metadata["max_turns_source"] == "family_default"
