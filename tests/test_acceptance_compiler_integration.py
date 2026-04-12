"""Integration tests for acceptance_compiler.

These tests execute the generated shell snippets via subprocess against
real (but minimal) filesystem fixtures, verifying that the expected
artifacts land under the workspace directory.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from acceptance.loader import Criterion
from codeprobe.acceptance_compiler import compile_actions


def _criterion(
    id: str,
    check_type: str,
    params: dict,
    tier: str = "behavioral",
) -> Criterion:
    return Criterion(
        id=id,
        description="integration test criterion",
        tier=tier,
        check_type=check_type,
        severity="high",
        prd_source="docs/prd/test.md",
        depends_on=(),
        params=params,
    )


class TestCliExitCodeIntegration:
    """Execute a cli_exit_code snippet that runs a command expected to fail."""

    def test_nonexistent_path_produces_nonzero_exit(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        target_repo = tmp_path / "repo"
        target_repo.mkdir()

        c = _criterion(
            id="BUG-INTERPRET-EXIT-004",
            check_type="cli_exit_code",
            params={
                "command": "ls /nonexistent_path_that_does_not_exist_12345",
                "expected_exit_not": 0,
            },
        )
        actions = compile_actions(
            [c],
            target_repo=target_repo,
            workspace=workspace,
            project_root=tmp_path,
        )
        assert len(actions) == 1

        result = subprocess.run(
            ["bash", "-c", actions[0].shell_snippet],
            cwd=str(workspace),
            timeout=10,
        )
        # The outer script always succeeds (echo $? is the last command)
        assert result.returncode == 0

        exit_file = workspace / "BUG-INTERPRET-EXIT-004.exit"
        assert exit_file.is_file()
        exit_code = int(exit_file.read_text().strip())
        assert exit_code != 0, "Expected non-zero exit for nonexistent path"

        stdout_file = workspace / "BUG-INTERPRET-EXIT-004.stdout"
        stderr_file = workspace / "BUG-INTERPRET-EXIT-004.stderr"
        assert stdout_file.is_file()
        assert stderr_file.is_file()


class TestCliWritesFileIntegration:
    """Execute a cli_writes_file snippet and verify the expected file lands."""

    def test_touch_command_creates_file(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        target_repo = tmp_path / "repo"
        target_repo.mkdir()

        c = _criterion(
            id="BUG-INIT-DEFAULT-006",
            check_type="cli_writes_file",
            params={
                "command": "mkdir -p .codeprobe && echo '{}' > .codeprobe/experiment.json",
                "expected_path": ".codeprobe/experiment.json",
            },
        )
        actions = compile_actions(
            [c],
            target_repo=target_repo,
            workspace=workspace,
            project_root=tmp_path,
        )
        assert len(actions) == 1

        result = subprocess.run(
            ["bash", "-c", actions[0].shell_snippet],
            cwd=str(workspace),
            timeout=10,
        )
        assert result.returncode == 0

        expected = workspace / ".codeprobe" / "experiment.json"
        assert expected.is_file(), f"Expected {expected} to exist"
        assert expected.read_text().strip() == "{}"


class TestSyncActionIntegration:
    """Execute a count_ge sync snippet and verify .codeprobe is copied."""

    def test_sync_copies_codeprobe_dir(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        target_repo = tmp_path / "repo"
        target_repo.mkdir()

        # Create fake .codeprobe/tasks structure in target_repo
        tasks_dir = target_repo / ".codeprobe" / "tasks"
        tasks_dir.mkdir(parents=True)
        (tasks_dir / "task-001").mkdir()
        (tasks_dir / "task-002").mkdir()
        (tasks_dir / "task-003").mkdir()

        c = _criterion(
            id="SILENT-MINE-COUNT-001",
            check_type="count_ge",
            tier="statistical",
            params={
                "source": "{repo}/.codeprobe/tasks",
                "pattern": "task-*",
                "min_count": 3,
            },
        )
        actions = compile_actions(
            [c],
            target_repo=target_repo,
            workspace=workspace,
            project_root=tmp_path,
        )
        assert len(actions) == 1

        result = subprocess.run(
            ["bash", "-c", actions[0].shell_snippet],
            cwd=str(workspace),
            timeout=10,
        )
        assert result.returncode == 0

        synced_tasks = workspace / ".codeprobe" / "tasks"
        assert synced_tasks.is_dir()
        assert len(list(synced_tasks.glob("task-*"))) == 3
