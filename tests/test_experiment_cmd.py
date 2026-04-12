"""Tests for codeprobe experiment CLI command group."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.core.experiment import (
    create_experiment_dir,
    load_experiment,
    save_config_results,
)
from codeprobe.models.experiment import (
    CompletedTask,
    Experiment,
    ExperimentConfig,
)


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture()
def exp_dir(tmp_path: Path) -> Path:
    """Create a minimal experiment directory with tasks and configs."""
    exp = Experiment(
        name="test-exp",
        description="A test experiment",
        configs=[
            ExperimentConfig(label="baseline", agent="claude"),
            ExperimentConfig(
                label="variant", agent="claude", model="claude-sonnet-4-6"
            ),
        ],
        tasks_dir="tasks",
    )
    d = create_experiment_dir(tmp_path, exp)
    # Create two task directories with instruction.md and test.sh
    for tid in ("task-001", "task-002"):
        task_dir = d / "tasks" / tid
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "instruction.md").write_text(f"# {tid}\nDo something.\n")
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        (tests_dir / "test.sh").write_text("#!/bin/bash\nexit 0\n")
    return d


# ---- experiment command is registered ----


def test_experiment_command_registered(runner: CliRunner) -> None:
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "experiment" in result.output


def test_experiment_help(runner: CliRunner) -> None:
    result = runner.invoke(main, ["experiment", "--help"])
    assert result.exit_code == 0
    for sub in ("init", "add-config", "validate", "status", "aggregate"):
        assert sub in result.output, f"Subcommand '{sub}' not in experiment help"


# ---- init ----


def test_init_creates_experiment(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        main,
        [
            "experiment",
            "init",
            str(tmp_path),
            "--name",
            "my-exp",
            "--description",
            "testing",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "my-exp" in result.output

    exp_dir = tmp_path / "my-exp"
    assert exp_dir.is_dir()
    assert (exp_dir / "experiment.json").is_file()
    assert (exp_dir / "tasks").is_dir()

    loaded = load_experiment(exp_dir)
    assert loaded.name == "my-exp"
    assert loaded.description == "testing"


def test_init_fails_if_exists(runner: CliRunner, exp_dir: Path) -> None:
    parent = exp_dir.parent
    result = runner.invoke(
        main,
        ["experiment", "init", str(parent), "--name", "test-exp"],
    )
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_init_rejects_unsafe_name(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(
        main,
        ["experiment", "init", str(tmp_path), "--name", "../traversal"],
    )
    assert result.exit_code == 1
    assert "Unsafe" in result.output or "error" in result.output.lower()


# ---- init --non-interactive (BUG-INIT-DEFAULT-006) ----


def test_init_non_interactive_creates_experiment_json(
    runner: CliRunner, tmp_path: Path
) -> None:
    """--non-interactive writes .codeprobe/experiment.json inside the target path."""
    result = runner.invoke(
        main,
        ["experiment", "init", str(tmp_path), "--non-interactive"],
    )
    assert result.exit_code == 0, result.output

    codeprobe_dir = tmp_path / ".codeprobe"
    exp_json = codeprobe_dir / "experiment.json"
    assert codeprobe_dir.is_dir(), ".codeprobe/ directory not created"
    assert exp_json.is_file(), ".codeprobe/experiment.json not created"
    assert (codeprobe_dir / "tasks").is_dir(), ".codeprobe/tasks/ not created"

    loaded = load_experiment(codeprobe_dir)
    assert loaded.name == "default"


def test_init_non_interactive_with_custom_name(
    runner: CliRunner, tmp_path: Path
) -> None:
    """--non-interactive respects --name when provided."""
    result = runner.invoke(
        main,
        [
            "experiment",
            "init",
            str(tmp_path),
            "--non-interactive",
            "--name",
            "custom-exp",
        ],
    )
    assert result.exit_code == 0, result.output

    loaded = load_experiment(tmp_path / ".codeprobe")
    assert loaded.name == "custom-exp"


def test_init_non_interactive_fails_if_already_exists(
    runner: CliRunner, tmp_path: Path
) -> None:
    """--non-interactive refuses to overwrite an existing experiment."""
    # First init
    runner.invoke(
        main,
        ["experiment", "init", str(tmp_path), "--non-interactive"],
    )
    # Second init should fail
    result = runner.invoke(
        main,
        ["experiment", "init", str(tmp_path), "--non-interactive"],
    )
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_init_non_interactive_experiment_json_is_valid(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The generated experiment.json is valid JSON with expected structure."""
    result = runner.invoke(
        main,
        ["experiment", "init", str(tmp_path), "--non-interactive"],
    )
    assert result.exit_code == 0, result.output
    exp_json = tmp_path / ".codeprobe" / "experiment.json"
    data = json.loads(exp_json.read_text())
    assert "name" in data
    assert "configs" in data
    assert "tasks_dir" in data


# ---- add-config ----


def test_add_config(runner: CliRunner, exp_dir: Path) -> None:
    result = runner.invoke(
        main,
        [
            "experiment",
            "add-config",
            str(exp_dir),
            "--label",
            "with-mcp",
            "--agent",
            "claude",
            "--model",
            "claude-sonnet-4-6",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "with-mcp" in result.output

    loaded = load_experiment(exp_dir)
    labels = [c.label for c in loaded.configs]
    assert "with-mcp" in labels


def test_add_config_duplicate_label(runner: CliRunner, exp_dir: Path) -> None:
    result = runner.invoke(
        main,
        [
            "experiment",
            "add-config",
            str(exp_dir),
            "--label",
            "baseline",
            "--agent",
            "claude",
        ],
    )
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_add_config_rejects_unsafe_label(runner: CliRunner, exp_dir: Path) -> None:
    result = runner.invoke(
        main,
        [
            "experiment",
            "add-config",
            str(exp_dir),
            "--label",
            "../bad",
        ],
    )
    assert result.exit_code == 1


# ---- validate ----


def test_validate_ready(runner: CliRunner, exp_dir: Path) -> None:
    result = runner.invoke(main, ["experiment", "validate", str(exp_dir)])
    assert result.exit_code == 0
    assert "READY" in result.output


def test_validate_no_tasks(runner: CliRunner, tmp_path: Path) -> None:
    """Experiment with configs but no tasks should report errors."""
    exp = Experiment(
        name="empty-exp",
        configs=[ExperimentConfig(label="baseline")],
    )
    d = create_experiment_dir(tmp_path, exp)
    result = runner.invoke(main, ["experiment", "validate", str(d)])
    assert result.exit_code == 1
    assert "No tasks" in result.output


def test_validate_no_configs(runner: CliRunner, tmp_path: Path) -> None:
    """Experiment with tasks but no configs should report errors."""
    exp = Experiment(name="no-cfg-exp", configs=[])
    d = create_experiment_dir(tmp_path, exp)
    # Add a task
    task_dir = d / "tasks" / "t1"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("# Task\n")
    result = runner.invoke(main, ["experiment", "validate", str(d)])
    assert result.exit_code == 1
    assert "No configurations" in result.output


# ---- status ----


def test_status_shows_configs(runner: CliRunner, exp_dir: Path) -> None:
    result = runner.invoke(main, ["experiment", "status", str(exp_dir)])
    assert result.exit_code == 0
    assert "baseline" in result.output
    assert "variant" in result.output


def test_status_with_results(runner: CliRunner, exp_dir: Path) -> None:
    completed = [
        CompletedTask(task_id="task-001", automated_score=1.0, duration_seconds=2.0),
        CompletedTask(task_id="task-002", automated_score=0.5, duration_seconds=3.0),
    ]
    save_config_results(exp_dir, "baseline", completed)

    result = runner.invoke(main, ["experiment", "status", str(exp_dir)])
    assert result.exit_code == 0
    assert "baseline" in result.output


# ---- aggregate ----


def test_aggregate_produces_report(runner: CliRunner, exp_dir: Path) -> None:
    for label in ("baseline", "variant"):
        completed = [
            CompletedTask(
                task_id="task-001",
                automated_score=1.0 if label == "baseline" else 0.5,
                duration_seconds=2.0,
                cost_usd=0.10,
            ),
            CompletedTask(
                task_id="task-002",
                automated_score=0.5,
                duration_seconds=3.0,
                cost_usd=0.15,
            ),
        ]
        save_config_results(exp_dir, label, completed)

    result = runner.invoke(main, ["experiment", "aggregate", str(exp_dir)])
    assert result.exit_code == 0, result.output

    report_path = exp_dir / "reports" / "aggregate.json"
    assert report_path.is_file()

    report = json.loads(report_path.read_text())
    assert report["experiment"] == "test-exp"
    assert "config_summaries" in report
    assert "baseline" in report["config_summaries"]


def test_aggregate_no_results(runner: CliRunner, exp_dir: Path) -> None:
    """Aggregate with no results should still succeed (empty summaries)."""
    result = runner.invoke(main, ["experiment", "aggregate", str(exp_dir)])
    assert result.exit_code == 0


def test_aggregate_no_configs(runner: CliRunner, tmp_path: Path) -> None:
    exp = Experiment(name="nocfg", configs=[])
    d = create_experiment_dir(tmp_path, exp)
    result = runner.invoke(main, ["experiment", "aggregate", str(d)])
    assert result.exit_code == 1
    assert "at least 1 configuration" in result.output
