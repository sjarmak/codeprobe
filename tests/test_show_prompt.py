"""Tests for --show-prompt flag on codeprobe run."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main


def _setup_experiment(tmp_path: Path, *, preambles: tuple[str, ...] = ()) -> Path:
    """Create a minimal experiment directory with one task."""
    exp_dir = tmp_path / ".codeprobe" / "my-exp"
    tasks_dir = exp_dir / "tasks" / "task-001"
    tasks_dir.mkdir(parents=True)

    instruction = "Fix the bug in main.py by handling the KeyError."
    (tasks_dir / "instruction.md").write_text(instruction, encoding="utf-8")

    configs = [
        {
            "label": "baseline",
            "agent": "claude",
            "preambles": list(preambles),
        }
    ]
    experiment = {
        "name": "my-exp",
        "configs": configs,
        "tasks_dir": "tasks",
        "task_ids": ["task-001"],
    }
    (exp_dir / "experiment.json").write_text(json.dumps(experiment), encoding="utf-8")
    return exp_dir


def test_show_prompt_prints_instruction(tmp_path: Path) -> None:
    """--show-prompt prints the fully-resolved prompt including instruction text."""
    exp_dir = _setup_experiment(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        main, ["run", str(exp_dir), "--show-prompt"], catch_exceptions=False
    )

    assert result.exit_code == 0
    assert "Fix the bug in main.py" in result.output
    assert "You are working on the repository" in result.output


def test_show_prompt_with_preamble(tmp_path: Path) -> None:
    """--show-prompt resolves preamble templates into the output."""
    exp_dir = _setup_experiment(tmp_path, preambles=("custom",))

    # Create a custom preamble in the task's preambles dir
    task_dir = exp_dir / "tasks" / "task-001"
    preamble_dir = task_dir / "preambles"
    preamble_dir.mkdir()
    (preamble_dir / "custom.md").write_text(
        "You have access to repository at {{repo_path}}.", encoding="utf-8"
    )

    runner = CliRunner()
    result = runner.invoke(
        main, ["run", str(exp_dir), "--show-prompt"], catch_exceptions=False
    )

    assert result.exit_code == 0
    assert "Fix the bug in main.py" in result.output
    assert "You have access to repository at" in result.output


def test_show_prompt_no_tasks(tmp_path: Path) -> None:
    """--show-prompt exits with error when no tasks are found."""
    exp_dir = tmp_path / ".codeprobe" / "empty-exp"
    exp_dir.mkdir(parents=True)
    tasks_dir = exp_dir / "tasks"
    tasks_dir.mkdir()

    experiment = {
        "name": "empty-exp",
        "configs": [{"label": "baseline", "agent": "claude"}],
        "tasks_dir": "tasks",
    }
    (exp_dir / "experiment.json").write_text(json.dumps(experiment), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(main, ["run", str(exp_dir), "--show-prompt"])

    assert result.exit_code != 0
