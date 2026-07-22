"""Behavioral proof that no run path mutates the primary checkout.

codeprobe-f7rl.2 (locked decision 3): worktree isolation on EVERY run path,
including single-task/sequential; never git restore/clean/checkout the
customer's primary checkout. These tests use REAL git repos and REAL
WorktreeIsolation — no isolation fakes — so they fail if any run path
regresses to executing or resetting in repo_path.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path

from codeprobe.adapters.protocol import AgentConfig
from codeprobe.core.executor import execute_config, execute_task
from codeprobe.core.isolation import WorktreeIsolation
from codeprobe.models.experiment import ExperimentConfig
from tests.conftest import FakeAdapter


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> str:
    """Create a git repo with one commit; return the commit SHA."""
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "tracked.txt").write_text("original\n")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-q", "-m", "init")
    return _git(repo, "rev-parse", "HEAD")


def _make_task(task_dir: Path) -> Path:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text("Fix the bug.")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    test_sh = tests_dir / "test.sh"
    test_sh.write_text("#!/bin/bash\nexit 0\n")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)
    return task_dir


def test_sequential_run_never_mutates_primary_checkout(tmp_path: Path) -> None:
    """A parallel=1 run leaves uncommitted edits, untracked files, and
    HEAD/branch of the primary checkout untouched."""
    repo = tmp_path / "repo"
    head_before = _init_repo(repo)

    # Customer state that the old sequential path destroyed:
    # an uncommitted tracked edit and an untracked file.
    (repo / "tracked.txt").write_text("uncommitted edit\n")
    (repo / "untracked.txt").write_text("precious\n")

    task = _make_task(tmp_path / "task-000")
    adapter = FakeAdapter(stdout="output")

    results = execute_config(
        adapter=adapter,
        task_dirs=[task],
        repo_path=repo,
        experiment_config=ExperimentConfig(label="baseline"),
        agent_config=AgentConfig(),
        parallel=1,
    )

    assert len(results) == 1
    assert results[0].status == "completed", results[0].metadata

    # The agent ran inside a worktree slot, not the primary checkout.
    prompt = adapter.run_calls[0][0]
    assert f"repository at {repo}. Follow" not in prompt

    # Primary checkout untouched.
    assert (repo / "tracked.txt").read_text() == "uncommitted edit\n"
    assert (repo / "untracked.txt").read_text() == "precious\n"
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "main"


def test_execute_task_pins_in_owned_worktree(tmp_path: Path) -> None:
    """A pinning task without a caller-supplied worktree acquires its own —
    git_pin_commit never moves the primary checkout's HEAD."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "second.txt").write_text("two\n")
    _git(repo, "add", "second.txt")
    _git(repo, "commit", "-q", "-m", "second")
    head_before = _git(repo, "rev-parse", "HEAD")

    task = _make_task(tmp_path / "task-pin")
    (task / "metadata.json").write_text(
        json.dumps(
            {
                "id": "task-pin",
                "metadata": {"ground_truth_commit": head_before},
                "verification": {
                    "type": "test_script",
                    "command": "bash tests/test.sh",
                },
            }
        )
    )

    adapter = FakeAdapter(stdout="output")
    result = execute_task(
        adapter=adapter,
        task_dir=task,
        repo_path=repo,
        agent_config=AgentConfig(),
        worktree_path=None,
    )

    assert result.completed.status == "completed", result.completed.metadata

    # The pin happened in an acquired worktree; the primary checkout keeps
    # its HEAD and its branch.
    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    prompt = adapter.run_calls[0][0]
    assert f"repository at {repo}. Follow" not in prompt


def test_worktree_release_removes_multi_repo_layout(tmp_path: Path) -> None:
    """Slot reset removes workspace/repos/ (nested git clones survive a
    plain ``git clean -fd``) so multi-repo layouts can't leak across tasks."""
    repo = tmp_path / "repo"
    _init_repo(repo)

    iso = WorktreeIsolation(repo, pool_size=1, namespace="safety")
    try:
        wt = iso.acquire()
        nested = wt / "repos" / "repoB"
        _init_repo(nested)
        assert (nested / ".git").exists()

        iso.release(wt)
        assert not (wt / "repos").exists()
    finally:
        iso.cleanup()
