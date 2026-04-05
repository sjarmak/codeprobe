"""Tests for core/experiment.py — experiment directory I/O."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.core.experiment import (
    append_checkpoint,
    create_experiment_dir,
    load_checkpoint,
    load_config_results,
    load_experiment,
    save_config_results,
    save_experiment,
)
from codeprobe.models.experiment import (
    CompletedTask,
    ConfigResults,
    Experiment,
    ExperimentConfig,
)


def _sample_experiment() -> Experiment:
    return Experiment(
        name="test-exp",
        description="A test experiment",
        configs=[
            ExperimentConfig(label="baseline"),
            ExperimentConfig(label="variant", model="claude-sonnet-4-6"),
        ],
        tasks_dir="tasks",
    )


def test_create_experiment_dir(tmp_path: Path):
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    assert exp_dir.is_dir()
    assert (exp_dir / "tasks").is_dir()
    assert (exp_dir / "runs" / "baseline").is_dir()
    assert (exp_dir / "runs" / "variant").is_dir()
    assert (exp_dir / "experiment.json").is_file()

    data = json.loads((exp_dir / "experiment.json").read_text())
    assert data["name"] == "test-exp"


def test_save_and_load_experiment(tmp_path: Path):
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    loaded = load_experiment(exp_dir)
    assert loaded.name == exp.name
    assert loaded.description == exp.description
    assert len(loaded.configs) == 2
    assert loaded.configs[0].label == "baseline"


def test_load_experiment_missing_raises(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_experiment(tmp_path / "nonexistent")


def test_save_experiment_overwrites(tmp_path: Path):
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    updated = Experiment(
        name="test-exp",
        description="Updated description",
        configs=exp.configs,
    )
    save_experiment(exp_dir, updated)
    loaded = load_experiment(exp_dir)
    assert loaded.description == "Updated description"


def test_save_and_load_config_results(tmp_path: Path):
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    completed = [
        CompletedTask(task_id="t-001", automated_score=1.0, duration_seconds=2.5),
        CompletedTask(task_id="t-002", automated_score=0.0, duration_seconds=1.0),
    ]

    path = save_config_results(exp_dir, "baseline", completed)
    assert path.is_file()

    loaded = load_config_results(exp_dir, "baseline")
    assert isinstance(loaded, ConfigResults)
    assert loaded.config == "baseline"
    assert len(loaded.completed) == 2
    assert loaded.completed[0].task_id == "t-001"
    assert loaded.completed[0].automated_score == 1.0


def test_load_config_results_missing_raises(tmp_path: Path):
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    with pytest.raises(FileNotFoundError):
        load_config_results(exp_dir, "nonexistent")


def test_save_and_load_task_ids(tmp_path: Path):
    """Experiment.task_ids round-trips through save/load."""
    exp = Experiment(
        name="scoped-exp",
        description="Experiment with scoped tasks",
        configs=[ExperimentConfig(label="baseline")],
        task_ids=("aaa111", "bbb222"),
    )
    exp_dir = create_experiment_dir(tmp_path, exp)

    loaded = load_experiment(exp_dir)
    assert loaded.task_ids == ("aaa111", "bbb222")


def test_load_experiment_without_task_ids(tmp_path: Path):
    """Old experiment.json without task_ids loads with empty tuple."""
    exp = _sample_experiment()
    exp_dir = create_experiment_dir(tmp_path, exp)

    # Manually strip task_ids from the JSON to simulate old format
    path = exp_dir / "experiment.json"
    data = json.loads(path.read_text())
    data.pop("task_ids", None)
    path.write_text(json.dumps(data))

    loaded = load_experiment(exp_dir)
    assert loaded.task_ids == ()


def test_task_ids_filters_discovery(tmp_path: Path):
    """When task_ids is set, only those tasks are discovered by _find_tasks."""
    from codeprobe.cli.run_cmd import _find_tasks

    # Create 3 task dirs
    tasks_dir = tmp_path / "tasks"
    for tid in ("aaa111", "bbb222", "ccc333"):
        d = tasks_dir / tid
        d.mkdir(parents=True)
        (d / "instruction.md").write_text("do something")

    # Without filter: all 3
    all_tasks = _find_tasks(tasks_dir)
    assert len(all_tasks) == 3

    # With filter: only 2
    filtered = _find_tasks(tasks_dir, task_ids=("aaa111", "ccc333"))
    assert [d.name for d in filtered] == ["aaa111", "ccc333"]


def test_task_ids_filters_ignores_missing(tmp_path: Path):
    """task_ids referencing non-existent dirs are silently skipped."""
    from codeprobe.cli.run_cmd import _find_tasks

    tasks_dir = tmp_path / "tasks"
    d = tasks_dir / "aaa111"
    d.mkdir(parents=True)
    (d / "instruction.md").write_text("do something")

    filtered = _find_tasks(tasks_dir, task_ids=("aaa111", "missing999"))
    assert [d.name for d in filtered] == ["aaa111"]


def test_record_task_ids_in_experiment(tmp_path: Path):
    """_record_task_ids_in_experiment updates experiment.json with task IDs."""
    from codeprobe.cli.mine_cmd import _record_task_ids_in_experiment

    # Set up repo with a single experiment
    codeprobe_dir = tmp_path / ".codeprobe"
    exp = Experiment(
        name="my-exp",
        configs=[ExperimentConfig(label="baseline")],
    )
    exp_dir = create_experiment_dir(codeprobe_dir, exp)

    # Record task IDs
    _record_task_ids_in_experiment(tmp_path, ["ccc333", "aaa111", "bbb222"])

    loaded = load_experiment(exp_dir)
    assert loaded.task_ids == ("aaa111", "bbb222", "ccc333")  # sorted


def test_record_task_ids_skips_multiple_experiments(tmp_path: Path):
    """No update when multiple experiments exist (ambiguous)."""
    from codeprobe.cli.mine_cmd import _record_task_ids_in_experiment

    codeprobe_dir = tmp_path / ".codeprobe"
    for name in ("exp-a", "exp-b"):
        create_experiment_dir(
            codeprobe_dir,
            Experiment(name=name, configs=[ExperimentConfig(label="base")]),
        )

    _record_task_ids_in_experiment(tmp_path, ["task1"])

    # Neither experiment should have task_ids set
    for name in ("exp-a", "exp-b"):
        loaded = load_experiment(codeprobe_dir / name)
        assert loaded.task_ids == ()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_append_and_load_checkpoint(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.jsonl"

    t1 = CompletedTask(task_id="t-001", automated_score=1.0)
    t2 = CompletedTask(task_id="t-002", automated_score=0.0)

    append_checkpoint(checkpoint, t1)
    append_checkpoint(checkpoint, t2)

    ids = load_checkpoint(checkpoint)
    assert ids == {"t-001", "t-002"}


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_load_checkpoint_empty(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.jsonl"
    ids = load_checkpoint(checkpoint)
    assert ids == set()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_load_checkpoint_skips_malformed(tmp_path: Path):
    checkpoint = tmp_path / "checkpoint.jsonl"
    checkpoint.write_text(
        '{"task_id": "t-001"}\n' "not valid json\n" '{"task_id": "t-002"}\n'
    )
    ids = load_checkpoint(checkpoint)
    assert ids == {"t-001", "t-002"}
