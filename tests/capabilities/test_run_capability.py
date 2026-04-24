"""End-to-end capability coverage for ``codeprobe run``.

Run executes tasks against an agent and produces scored results. To keep the
test matrix fast and reliable, the agent adapter is replaced at the registry
boundary by the existing ``FakeAdapter`` (see ``tests/conftest.py``). The
pipeline — experiment loading, task discovery, worktree isolation, test.sh
execution, scoring, checkpointing — executes for real.

Matrix cells:
  - synthetic python task directory  / test_script scoring
  - synthetic python task directory  / --dry-run cost estimate
  - CLI surface: ``run --help`` shape
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeprobe.adapters.protocol import AgentConfig
from codeprobe.core.executor import execute_config
from codeprobe.models.experiment import ExperimentConfig
from tests.conftest import FakeAdapter

pytestmark = [pytest.mark.capability]


@pytest.fixture()
def seeded_task_repo(make_task_dir, tmp_path: Path) -> tuple[Path, list[Path]]:
    """Return (repo_root, [task_dirs]) with a minimal git repo and 2 tasks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "r@example.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "r"], check=True, capture_output=True)
    (repo / "README.md").write_text("seed\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"], check=True, capture_output=True)

    t1 = make_task_dir("task-a", language="python", passing=True)
    t2 = make_task_dir("task-b", language="python", passing=True)
    return repo, [t1, t2]


@pytest.mark.matrix
def test_run_executes_tasks_via_fake_adapter(
    seeded_task_repo: tuple[Path, list[Path]],
) -> None:
    """Execute two tasks with a FakeAdapter; assert structural shape of results."""
    repo, tasks = seeded_task_repo
    adapter = FakeAdapter(
        stdout="ok", cost_usd=0.01, cost_model="per_token", duration=0.1
    )
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=repo,
        experiment_config=exp_config,
        agent_config=agent_config,
    )

    assert len(results) == len(tasks), (
        f"capability=run fixture=synthetic/2-tasks expected {len(tasks)} results; "
        f"got {len(results)}"
    )
    for r in results:
        assert r.task_id, "CompletedTask.task_id empty"
        assert isinstance(r.automated_score, float), (
            f"capability=run task_id={r.task_id} automated_score not float: "
            f"{type(r.automated_score).__name__}"
        )
        assert isinstance(r.duration_seconds, (int, float)), (
            f"capability=run task_id={r.task_id} duration_seconds not numeric"
        )
    # Adapter was actually called (not silently short-circuited)
    assert len(adapter.run_calls) == len(tasks), (
        f"capability=run expected adapter.run called {len(tasks)}× "
        f"got {len(adapter.run_calls)}"
    )


@pytest.mark.matrix
def test_run_passing_tests_score_is_not_negative(
    seeded_task_repo: tuple[Path, list[Path]],
) -> None:
    """test.sh exits 0 for both tasks → score must be ≥ 0 and ≤ 1 (structural)."""
    repo, tasks = seeded_task_repo
    adapter = FakeAdapter(stdout="ok", cost_usd=0.0, cost_model="unknown", duration=0.05)
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=repo,
        experiment_config=exp_config,
        agent_config=agent_config,
    )

    assert all(0.0 <= r.automated_score <= 1.0 for r in results), (
        f"capability=run all scores expected in [0,1]; got "
        f"{[(r.task_id, r.automated_score) for r in results]}"
    )


def test_run_respects_budget_halt(
    seeded_task_repo: tuple[Path, list[Path]],
) -> None:
    """Budget exceeded in sequential mode halts execution before all tasks complete."""
    repo, tasks = seeded_task_repo
    # Cost per task > budget → should halt after the first task.
    adapter = FakeAdapter(stdout="ok", cost_usd=1.0, cost_model="per_token", duration=0.01)
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=repo,
        experiment_config=exp_config,
        agent_config=agent_config,
        max_cost_usd=0.5,
    )

    # Tight: at least one task ran, but the second should have been skipped.
    assert 1 <= len(results) < len(tasks), (
        f"capability=run budget-halt expected partial results "
        f"(1 <= n < {len(tasks)}); got {len(results)}"
    )
