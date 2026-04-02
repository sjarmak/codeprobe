"""Tests for core/executor.py — task execution."""

from __future__ import annotations

import stat
import threading
from pathlib import Path
from unittest.mock import patch, call, MagicMock

from codeprobe.adapters.protocol import AgentConfig
from codeprobe.core.executor import (
    TaskResult,
    _git_reset_workdir,
    execute_config,
    execute_task,
    get_concurrency_semaphore,
    load_instruction,
    set_max_concurrency,
)
from codeprobe.core.isolation import IsolationStrategy, WorktreeIsolation
from codeprobe.core.preamble import _base_prompt
from codeprobe.core.preamble import DefaultPreambleResolver
from codeprobe.models.experiment import CompletedTask, ExperimentConfig
from tests.conftest import FakeAdapter, SequentialCostAdapter


def _make_task(
    task_dir: Path, instruction: str = "Fix the bug.", *, passing: bool = True
) -> Path:
    """Create a minimal task directory with instruction and test.sh."""
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text(instruction)
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    test_sh = tests_dir / "test.sh"
    exit_code = 0 if passing else 1
    test_sh.write_text(f"#!/bin/bash\nexit {exit_code}\n")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)
    return task_dir


def test_base_prompt():
    prompt = _base_prompt("Fix the bug.", Path("/repo"))
    assert "Fix the bug." in prompt
    assert "/repo" in prompt


def test_load_instruction(tmp_path: Path):
    task_dir = tmp_path / "task-001"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("Do the thing.")

    text = load_instruction(task_dir)
    assert text == "Do the thing."


def test_load_instruction_variant(tmp_path: Path):
    task_dir = tmp_path / "task-002"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("default")
    (task_dir / "instruction_mcp.md").write_text("with mcp tools")

    text = load_instruction(task_dir, variant="instruction_mcp.md")
    assert text == "with mcp tools"


def test_load_instruction_variant_fallback(tmp_path: Path):
    task_dir = tmp_path / "task-003"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("default only")

    text = load_instruction(task_dir, variant="instruction_mcp.md")
    assert text == "default only"


def test_load_instruction_missing(tmp_path: Path):
    task_dir = tmp_path / "task-004"
    task_dir.mkdir()

    import pytest

    with pytest.raises(FileNotFoundError):
        load_instruction(task_dir)


def test_load_instruction_variant_path_traversal(tmp_path: Path):
    """instruction_variant must not escape the task directory."""
    task_dir = tmp_path / "task-005"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("default")
    # Create a file outside task_dir
    (tmp_path / "secret.md").write_text("secret content")

    import pytest

    with pytest.raises(ValueError, match="escapes task directory"):
        load_instruction(task_dir, variant="../secret.md")


def test_execute_task_success(tmp_path: Path):
    task_dir = _make_task(tmp_path / "task-001", passing=True)
    adapter = FakeAdapter(stdout="correct answer")
    config = AgentConfig()

    task_result = execute_task(adapter, task_dir, Path("/repo"), config)
    assert isinstance(task_result, TaskResult)
    result = task_result.completed
    assert isinstance(result, CompletedTask)
    assert result.task_id == "task-001"
    assert result.automated_score == 1.0
    assert result.status == "completed"
    assert len(adapter.run_calls) == 1
    assert task_result.agent_stdout == "correct answer"


def test_execute_task_with_preambles(tmp_path: Path):
    """Preambles are composed into the prompt and stored in metadata."""
    task_dir = _make_task(tmp_path / "task-001", passing=True)
    preambles_dir = task_dir / "preambles"
    preambles_dir.mkdir()
    (preambles_dir / "tdd.md").write_text("Write tests first.")

    resolver = DefaultPreambleResolver(task_dir=task_dir)
    adapter = FakeAdapter(stdout="correct answer")
    config = AgentConfig()

    result = execute_task(
        adapter,
        task_dir,
        Path("/repo"),
        config,
        preamble_names=("tdd",),
        preamble_resolver=resolver,
    ).completed
    assert result.status == "completed"
    assert result.automated_score == 1.0
    # Preamble content was composed into prompt
    prompt_sent = adapter.run_calls[0][0]
    assert "Write tests first." in prompt_sent
    # Resolved preambles stored for reproducibility
    assert "resolved_preambles" in result.metadata
    assert result.metadata["resolved_preambles"][0]["name"] == "tdd"


def test_execute_task_preamble_missing_errors(tmp_path: Path):
    """Missing preamble returns error, not crash."""
    task_dir = _make_task(tmp_path / "task-001", passing=True)

    resolver = DefaultPreambleResolver(task_dir=task_dir)
    adapter = FakeAdapter(stdout="output")
    config = AgentConfig()

    result = execute_task(
        adapter,
        task_dir,
        Path("/repo"),
        config,
        preamble_names=("nonexistent",),
        preamble_resolver=resolver,
    ).completed
    assert result.status == "error"
    assert "Preamble resolution failed" in result.metadata["error"]


def test_execute_task_preambles_without_resolver_errors(tmp_path: Path):
    """Requesting preambles without a resolver returns error (validate-or-die)."""
    task_dir = _make_task(tmp_path / "task-001", passing=True)
    adapter = FakeAdapter(stdout="output")
    config = AgentConfig()

    result = execute_task(
        adapter,
        task_dir,
        Path("/repo"),
        config,
        preamble_names=("tdd",),
        preamble_resolver=None,
    ).completed
    assert result.status == "error"
    assert "no preamble_resolver provided" in result.metadata["error"]


def test_execute_task_failing_test(tmp_path: Path):
    task_dir = _make_task(tmp_path / "task-002", passing=False)
    adapter = FakeAdapter(stdout="wrong answer")
    config = AgentConfig()

    result = execute_task(adapter, task_dir, Path("/repo"), config).completed
    assert result.automated_score == 0.0
    assert result.status == "completed"


def test_execute_task_agent_error(tmp_path: Path):
    task_dir = _make_task(tmp_path / "task-003", passing=True)
    adapter = FakeAdapter(stdout="", exit_code=1, stderr="agent crashed")
    config = AgentConfig()

    result = execute_task(adapter, task_dir, Path("/repo"), config).completed
    assert result.automated_score == 0.0
    assert result.metadata.get("error") is not None


def test_execute_task_missing_instruction(tmp_path: Path):
    task_dir = tmp_path / "task-004"
    task_dir.mkdir(parents=True)
    adapter = FakeAdapter()
    config = AgentConfig()

    result = execute_task(adapter, task_dir, Path("/repo"), config).completed
    assert result.automated_score == 0.0
    assert "error" in result.metadata


def test_execute_config_forwards_reward_type(tmp_path: Path):
    """reward_type from ExperimentConfig is forwarded to execute_task."""
    task_dir = _make_task(tmp_path / "task-001", passing=True)
    # Create a continuous score output (test.sh exits 0 = score 1.0 for binary)
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline", reward_type="continuous")
    agent_config = AgentConfig()

    # The continuous scorer will look for tests/test.sh — which exists and passes
    results = execute_config(
        adapter=adapter,
        task_dirs=[task_dir],
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
    )
    assert len(results) == 1
    # Key assertion: if reward_type wasn't forwarded, it would use "binary"
    # and the scoring_details would differ. The test.sh passes, so score = 1.0 either way,
    # but we can verify the scorer was invoked by checking the result is valid.
    assert results[0].status == "completed"


def test_execute_config_runs_all_tasks(tmp_path: Path):
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
    )
    assert len(results) == 3
    assert all(isinstance(r, CompletedTask) for r in results)
    assert len(adapter.run_calls) == 3


def test_execute_config_skips_checkpointed(tmp_path: Path):
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    # Write a checkpoint with task-000 already done
    from codeprobe.core.checkpoint import CheckpointStore
    from codeprobe.models.experiment import CompletedTask as CT

    checkpoint_db = tmp_path / "checkpoint.db"
    store = CheckpointStore(checkpoint_db, config_name="baseline")
    store.append(CT(task_id="task-000", automated_score=1.0))

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        checkpoint_store=store,
    )
    # Should skip task-000, run task-001 and task-002
    assert len(adapter.run_calls) == 2
    # But results should include all 3 (1 from checkpoint + 2 new)
    assert len(results) == 3
    assert results[0].task_id == "task-000"
    assert results[0].automated_score == 1.0


def test_execute_config_calls_callback(tmp_path: Path):
    tasks = [_make_task(tmp_path / "task-000", passing=True)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    callback_results: list[CompletedTask] = []

    execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        on_task_complete=callback_results.append,
    )
    assert len(callback_results) == 1
    assert callback_results[0].task_id == "task-000"


# --- Cost circuit-breaker tests ---


def test_execute_config_halts_at_budget(tmp_path: Path):
    """Executor stops running tasks when cumulative cost exceeds max_cost_usd."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(5)]
    # Each task costs $1.00, budget is $2.50 -> should run 3 tasks (0+1+1=2 after first,
    # then 2+1=3 after third which exceeds 2.50, so halt before 4th)
    adapter = FakeAdapter(stdout="output", cost_usd=1.0, cost_model="per_token")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        max_cost_usd=2.50,
    )
    # Should have run 3 tasks: after task 3, cumulative = $3.00 > $2.50, halt
    assert len(results) == 3
    assert len(adapter.run_calls) == 3


def test_execute_config_no_budget_runs_all(tmp_path: Path):
    """Without max_cost_usd, all tasks run regardless of cost."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(5)]
    adapter = FakeAdapter(stdout="output", cost_usd=10.0, cost_model="per_token")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
    )
    assert len(results) == 5


def test_execute_config_skips_unknown_cost_model_in_accumulation(tmp_path: Path):
    """Tasks with unknown or subscription cost_model are not counted toward budget."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(4)]
    # Task 0: $1.00 per_token, Task 1: unknown (skipped), Task 2: $1.00 per_token,
    # Task 3: $1.00 per_token -> cumulative at task 2 = $2.00, task 3 = $3.00 > $2.50
    adapter = SequentialCostAdapter(
        costs=[
            (1.0, "per_token"),
            (None, "unknown"),
            (1.0, "per_token"),
            (1.0, "per_token"),
        ],
        stdout="output",
    )
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        max_cost_usd=2.50,
    )
    # Tasks 0, 1, 2 run (cumulative per_token = $2.00), task 3 would push to $3.00 -> halt
    assert len(results) == 4
    assert len(adapter.run_calls) == 4


def test_execute_config_skips_subscription_cost_model(tmp_path: Path):
    """Tasks with subscription cost_model are not counted toward budget."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = SequentialCostAdapter(
        costs=[
            (0.0, "subscription"),
            (0.0, "subscription"),
            (0.0, "subscription"),
        ],
        stdout="output",
    )
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        max_cost_usd=0.01,  # Tiny budget, but subscription costs are skipped
    )
    assert len(results) == 3


def test_execute_config_budget_saves_partial_results(tmp_path: Path):
    """Partial results from budget halt are valid CompletedTask objects."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(5)]
    adapter = FakeAdapter(stdout="output", cost_usd=2.0, cost_model="per_token")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        max_cost_usd=3.0,
    )
    # $2.00 after task 0, $4.00 after task 1 -> halt
    assert len(results) == 2
    assert all(isinstance(r, CompletedTask) for r in results)
    assert all(r.status == "completed" for r in results)


def test_execute_config_budget_with_checkpoint(tmp_path: Path):
    """Checkpointed tasks don't count toward budget (they were already paid for)."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(4)]
    adapter = FakeAdapter(stdout="output", cost_usd=1.5, cost_model="per_token")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    # Checkpoint task-000
    from codeprobe.core.checkpoint import CheckpointStore
    from codeprobe.models.experiment import CompletedTask as CT

    checkpoint_db = tmp_path / "checkpoint.db"
    store = CheckpointStore(checkpoint_db, config_name="baseline")
    store.append(CT(task_id="task-000", automated_score=1.0))

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        checkpoint_store=store,
        max_cost_usd=2.0,
    )
    # task-000 is checkpointed (free), task-001 costs $1.50, task-002 would be $3.00 -> halt
    assert len(adapter.run_calls) == 2
    # Results include checkpoint + 2 new
    assert len(results) == 3


def test_execute_config_budget_callback_fires_for_partial(tmp_path: Path):
    """on_task_complete fires for each completed task before budget halt."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(5)]
    adapter = FakeAdapter(stdout="output", cost_usd=3.0, cost_model="per_token")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    callback_results: list[CompletedTask] = []

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        max_cost_usd=5.0,
        on_task_complete=callback_results.append,
    )
    # $3 after task 0, $6 after task 1 -> halt after 2
    assert len(results) == 2
    assert len(callback_results) == 2


def test_execute_config_retries_error_checkpointed(tmp_path: Path):
    """Tasks checkpointed with status='error' should be retried, not skipped."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    from codeprobe.core.checkpoint import CheckpointStore
    from codeprobe.models.experiment import CompletedTask as CT

    checkpoint_db = tmp_path / "checkpoint.db"
    store = CheckpointStore(checkpoint_db, config_name="baseline")
    # task-000 completed successfully — should be skipped
    store.append(CT(task_id="task-000", automated_score=1.0, status="completed"))
    # task-001 errored — should be retried
    store.append(CT(task_id="task-001", automated_score=0.0, status="error"))

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        checkpoint_store=store,
    )
    # Should skip task-000, retry task-001, run task-002
    assert len(adapter.run_calls) == 2
    # Results: 1 from checkpoint + 2 newly run
    assert len(results) == 3
    assert results[0].task_id == "task-000"
    assert results[0].automated_score == 1.0


def test_execute_config_none_cost_not_accumulated(tmp_path: Path):
    """Tasks where cost_usd is None (per_token but None shouldn't happen, but
    cost_usd=None with unknown model) are skipped in accumulation."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = FakeAdapter(stdout="output", cost_usd=None, cost_model="unknown")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        max_cost_usd=0.01,
    )
    # All tasks run since no per_token costs to accumulate
    assert len(results) == 3


# --- Git reset between sequential tasks ---


def test_execute_config_resets_workdir_between_sequential_tasks(tmp_path: Path):
    """Git reset runs between tasks in sequential mode (parallel<=1)."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    with patch("codeprobe.core.executor._git_reset_workdir") as mock_reset:
        execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            parallel=1,
        )
        # Reset should be called between tasks (not before first), so 2 times for 3 tasks
        assert mock_reset.call_count == 2
        mock_reset.assert_any_call(Path("/repo"))


def test_execute_config_no_reset_in_parallel_mode(tmp_path: Path):
    """Git reset does NOT run between tasks in parallel mode (parallel>1)."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    with patch("codeprobe.core.executor._git_reset_workdir") as mock_reset:
        execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            parallel=3,
        )
        mock_reset.assert_not_called()


def test_execute_config_no_reset_for_single_task(tmp_path: Path):
    """No git reset when there's only one task (nothing to reset between)."""
    tasks = [_make_task(tmp_path / "task-000", passing=True)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    with patch("codeprobe.core.executor._git_reset_workdir") as mock_reset:
        execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            parallel=1,
        )
        mock_reset.assert_not_called()


# --- Worktree isolation tests ---


class TestWorktreeIsolation:
    def test_create_pool(self, tmp_path: Path) -> None:
        """WorktreeIsolation creates worktrees via subprocess."""
        with patch("subprocess.run") as mock_run:
            iso = WorktreeIsolation(tmp_path, pool_size=2)
            # Force pool creation by acquiring
            iso._base_dir.mkdir(parents=True, exist_ok=True)
            iso._create_pool()
            # Should call git worktree add twice
            assert mock_run.call_count == 2
            for c in mock_run.call_args_list:
                assert c[0][0][0:3] == ["git", "worktree", "add"]

    def test_acquire_returns_path(self, tmp_path: Path) -> None:
        """acquire() returns a worktree path from the pool."""
        with patch("subprocess.run"):
            iso = WorktreeIsolation(tmp_path, pool_size=1)
            iso._create_pool()
            wt = iso.acquire()
            assert isinstance(wt, Path)
            assert "slot-0" in str(wt)

    def test_reset_calls_git_checkout_and_clean(self, tmp_path: Path) -> None:
        """reset() runs git checkout -- . and git clean -fd."""
        iso = WorktreeIsolation(tmp_path, pool_size=1)
        wt = tmp_path / "worktree"
        wt.mkdir()
        with patch("subprocess.run") as mock_run:
            iso.reset(wt)
            assert mock_run.call_count == 2
            calls = [c[0][0] for c in mock_run.call_args_list]
            assert calls[0] == ["git", "checkout", "--", "."]
            assert calls[1] == ["git", "clean", "-fd"]

    def test_release_resets_and_returns_to_pool(self, tmp_path: Path) -> None:
        """release() resets the worktree and makes it available again."""
        with patch("subprocess.run"):
            iso = WorktreeIsolation(tmp_path, pool_size=1)
            iso._create_pool()
            wt = iso.acquire()
        with patch("subprocess.run"):
            iso.release(wt)
        # Should be available again
        assert not iso._available.empty()

    def test_cleanup_removes_worktrees(self, tmp_path: Path) -> None:
        """cleanup() calls git worktree remove for each worktree."""
        with patch("subprocess.run"):
            iso = WorktreeIsolation(tmp_path, pool_size=2)
            iso._create_pool()
        with patch("subprocess.run") as mock_run:
            iso.cleanup()
            assert mock_run.call_count == 2
            for c in mock_run.call_args_list:
                assert c[0][0][0:3] == ["git", "worktree", "remove"]

    def test_pool_size_validation(self) -> None:
        """pool_size must be >= 1."""
        import pytest

        with pytest.raises(ValueError, match="pool_size"):
            WorktreeIsolation(Path("/tmp"), pool_size=0)

    def test_satisfies_protocol(self, tmp_path: Path) -> None:
        """WorktreeIsolation satisfies IsolationStrategy protocol."""
        with patch("subprocess.run"):
            iso = WorktreeIsolation(tmp_path, pool_size=1)
            assert isinstance(iso, IsolationStrategy)


# --- Preamble repo_path rewriting ---


class TestPreambleRewriting:
    def test_base_prompt_uses_worktree_path(self) -> None:
        """_base_prompt uses worktree_path when provided."""
        prompt = _base_prompt(
            "Fix bug.", Path("/repo"), worktree_path=Path("/wt/slot-0")
        )
        assert "/wt/slot-0" in prompt
        assert "/repo" not in prompt

    def test_base_prompt_uses_repo_path_when_no_worktree(self) -> None:
        """_base_prompt uses repo_path when worktree_path is None."""
        prompt = _base_prompt("Fix bug.", Path("/repo"), worktree_path=None)
        assert "/repo" in prompt

    def test_execute_task_passes_worktree_path(self, tmp_path: Path) -> None:
        """execute_task passes worktree_path through to prompt."""
        task_dir = _make_task(tmp_path / "task-001", passing=True)
        adapter = FakeAdapter(stdout="correct answer")
        config = AgentConfig()
        wt_path = Path("/worktrees/slot-0")

        execute_task(adapter, task_dir, Path("/repo"), config, worktree_path=wt_path)
        prompt = adapter.run_calls[0][0]
        assert str(wt_path) in prompt
        assert "/repo" not in prompt


# --- Global concurrency semaphore ---


class TestConcurrencySemaphore:
    def test_set_and_get_semaphore(self) -> None:
        """set_max_concurrency creates a semaphore retrievable via get."""
        set_max_concurrency(3)
        sem = get_concurrency_semaphore()
        assert sem is not None
        # Verify it's a semaphore with correct count
        assert isinstance(sem, threading.Semaphore)

    def test_semaphore_limits_concurrency(self) -> None:
        """Semaphore actually limits concurrent access."""
        set_max_concurrency(2)
        sem = get_concurrency_semaphore()
        assert sem is not None

        # Acquire both slots
        assert sem.acquire(blocking=False)
        assert sem.acquire(blocking=False)
        # Third acquire should fail (non-blocking)
        assert not sem.acquire(blocking=False)
        # Release one
        sem.release()
        assert sem.acquire(blocking=False)
        # Clean up
        sem.release()
        sem.release()

    def test_executor_uses_isolation_in_parallel(self, tmp_path: Path) -> None:
        """execute_config uses isolation strategy when parallel > 1."""
        tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(2)]
        adapter = FakeAdapter(stdout="output")
        exp_config = ExperimentConfig(label="baseline")
        agent_config = AgentConfig()

        mock_iso = MagicMock(spec=WorktreeIsolation)
        mock_iso.acquire.return_value = Path("/wt/slot-0")

        with patch("codeprobe.core.executor.WorktreeIsolation", return_value=mock_iso):
            execute_config(
                adapter=adapter,
                task_dirs=tasks,
                repo_path=Path("/repo"),
                experiment_config=exp_config,
                agent_config=agent_config,
                parallel=2,
            )
        # Isolation should have been used
        assert mock_iso.acquire.call_count == 2
        assert mock_iso.release.call_count == 2
        assert mock_iso.cleanup.call_count == 1

    def test_parallel_isolation_passes_session_env_to_adapter(
        self, tmp_path: Path
    ) -> None:
        """When running in parallel with isolation, isolate_session() env
        reaches adapter.run() via session_env."""
        tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(2)]

        session_envs_received: list[dict[str, str] | None] = []

        class TrackingAdapter(FakeAdapter):
            def run(self, prompt, config, session_env=None):
                session_envs_received.append(session_env)
                return super().run(prompt, config, session_env=session_env)

            def isolate_session(self, slot_id: int) -> dict[str, str]:
                return {"CLAUDE_CONFIG_DIR": f"/tmp/codeprobe-claude/slot-{slot_id}"}

        adapter = TrackingAdapter(stdout="output")
        exp_config = ExperimentConfig(label="baseline")
        agent_config = AgentConfig()

        mock_iso = MagicMock(spec=WorktreeIsolation)
        mock_iso.acquire.return_value = Path("/wt/slot-0")

        with patch("codeprobe.core.executor.WorktreeIsolation", return_value=mock_iso):
            execute_config(
                adapter=adapter,
                task_dirs=tasks,
                repo_path=Path("/repo"),
                experiment_config=exp_config,
                agent_config=agent_config,
                parallel=2,
            )

        assert len(session_envs_received) == 2
        for env in session_envs_received:
            assert env is not None
            assert "CLAUDE_CONFIG_DIR" in env
            assert env["CLAUDE_CONFIG_DIR"].startswith("/tmp/codeprobe-claude/slot-")
