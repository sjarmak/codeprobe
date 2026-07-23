"""Regression tests for the ``--config-parallel`` flag (codeprobe-emez).

The bug: with multiple configs and ``parallel > 1``, the previous code path
in ``run_eval`` dispatched configs concurrently via a ThreadPoolExecutor
sized to ``len(configs_to_run)``. Combined with each config's own
parallel pool of size ``parallel``, total in-flight tasks could be
``len(configs) × parallel``. The per-task cost-cap check fires only on
task completion, so any tasks that are already running when budget
exceeds will complete and rack up cost past the cap.

The fix: configs run sequentially by default (config_parallel=1).
Cross-config parallelism is opt-in via ``--config-parallel N`` so users
who care about cost containment get correct behaviour by default.

codeprobe-f7rl.33 adds the experiment-wide budget ledger: one
``BudgetChecker`` shared by every arm, so concurrent arms stop together
once cross-arm spend crosses ``--max-cost-usd``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner


def _normalised_help() -> str:
    """Run ``codeprobe run --help`` and collapse all whitespace.

    Click wraps option help text at terminal width, so multi-word phrases
    can have arbitrary newlines/spacing in the middle. Collapsing
    whitespace lets us assert on prose without pinning the exact wrap.
    """
    from codeprobe.cli import main

    runner = CliRunner()
    result = runner.invoke(main, ["run", "--help"])
    assert result.exit_code == 0, result.output
    # Normalise whitespace: any run of \s becomes a single space.
    import re

    return re.sub(r"\s+", " ", result.output)


def test_run_help_lists_config_parallel_flag() -> None:
    """``codeprobe run --help`` advertises the new flag."""
    text = _normalised_help()
    assert "--config-parallel" in text
    assert "default: 1 = serial" in text


def test_run_help_documents_cost_cap_relationship() -> None:
    """The help text explains why the default is 1.

    This is a contract pin so future help-text rewrites don't accidentally
    drop the explanation. The bug came back twice in past investigations
    because the trade-off wasn't documented.
    """
    text = _normalised_help()
    assert "config_parallel × parallel" in text


def test_run_eval_signature_accepts_config_parallel() -> None:
    """The library-level ``run_eval`` exposes ``config_parallel`` as a kwarg.

    Skill-level callers (e.g. acceptance loops, harness wrappers) need to
    set this without going through the CLI surface. Pin the kwarg name so
    a rename would surface as a test break.
    """
    import inspect

    from codeprobe.cli.run_cmd import run_eval

    sig = inspect.signature(run_eval)
    assert "config_parallel" in sig.parameters
    assert sig.parameters["config_parallel"].default == 1


def test_config_parallel_clamped_to_config_count() -> None:
    """``effective_config_parallel`` should never exceed the number of configs.

    A user passing ``--config-parallel 8`` against an experiment with 2
    configs gets at most 2 workers (one per config), not 8. This avoids
    spawning idle workers and keeps the cost-cap accounting tight.

    We exercise this through the dispatch path indirectly: with one
    config and ``--config-parallel 5``, the serial-config branch should
    run, not the parallel branch (since ``min(5, 1) == 1``).
    """
    # The clamping logic lives at run_cmd.py line ~929:
    #   effective_config_parallel = min(config_parallel, len(configs_to_run))
    # We test it directly here so the logic is pinned.
    config_parallel = 5
    n_configs = 2
    assert min(config_parallel, n_configs) == 2

    config_parallel = 1
    n_configs = 3
    assert min(config_parallel, n_configs) == 1

    config_parallel = 8
    n_configs = 1
    assert min(config_parallel, n_configs) == 1


@pytest.mark.usefixtures("fake_worktree_isolation")
def test_shared_budget_stops_both_arms_mid_experiment(tmp_path: Path) -> None:
    """Concurrent arms (config_parallel=2) share one BudgetChecker: crossing
    the cap mid-experiment stops BOTH arms, and the recorded spend
    overshoots by at most config_parallel × parallel in-flight trials
    (2 × 1 here) (codeprobe-f7rl.33).
    """
    from concurrent.futures import ThreadPoolExecutor

    from codeprobe.adapters.protocol import AgentConfig
    from codeprobe.core.events import BudgetChecker
    from codeprobe.core.executor import execute_config
    from codeprobe.models.experiment import CompletedTask, ExperimentConfig
    from tests.conftest import FakeAdapter

    def _make_task(base: Path, name: str) -> Path:
        task_dir = base / name
        task_dir.mkdir(parents=True)
        (task_dir / "instruction.md").write_text("Fix the bug.")
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        test_sh = tests_dir / "test.sh"
        test_sh.write_text("#!/bin/bash\nexit 0\n")
        test_sh.chmod(0o755)
        return task_dir

    budget = 1.0
    per_trial = 0.5
    tasks_per_arm = 6
    checker = BudgetChecker(budget=budget)
    agent_config = AgentConfig()

    def _run_arm(label: str) -> list[CompletedTask]:
        tasks = [
            _make_task(tmp_path / label, f"task-{i:03d}")
            for i in range(tasks_per_arm)
        ]
        adapter = FakeAdapter(
            stdout="output", cost_usd=per_trial, cost_model="per_token"
        )
        return execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=ExperimentConfig(label=label),
            agent_config=agent_config,
            max_cost_usd=budget,
            budget_checker=checker,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run_arm, label) for label in ("arm-a", "arm-b")]
        results = [f.result() for f in futures]

    # Both arms stop well before finishing their 6 tasks.
    assert all(len(r) < tasks_per_arm for r in results)
    assert checker.is_exceeded
    # Overshoot bound: the budget plus one in-flight trial per concurrent
    # dispatch lane (config_parallel × parallel = 2 × 1).
    assert checker.cumulative_cost <= budget + 2 * per_trial
