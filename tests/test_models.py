"""Tests for core data models."""

from codeprobe.models.task import Task, TaskMetadata, TaskVerification
from codeprobe.models.experiment import Experiment, ExperimentConfig, CompletedTask, ConfigResults


def test_task_is_frozen():
    meta = TaskMetadata(name="test-task", difficulty="medium")
    task = Task(id="t-001", repo="org/repo", metadata=meta)
    assert task.id == "t-001"
    assert task.metadata.difficulty == "medium"


def test_experiment_config_defaults():
    config = ExperimentConfig(label="baseline")
    assert config.agent == "claude"
    assert config.model is None
    assert config.permission_mode == "default"
    assert config.mcp_config is None


def test_experiment_config_permission_mode():
    config = ExperimentConfig(label="safe", permission_mode="plan")
    assert config.permission_mode == "plan"


def test_completed_task_score():
    task = CompletedTask(task_id="t-001", automated_score=0.75)
    assert task.automated_score == 0.75
    assert task.status == "completed"


def test_config_results():
    results = ConfigResults(
        config="baseline",
        completed=[
            CompletedTask(task_id="t-001", automated_score=1.0),
            CompletedTask(task_id="t-002", automated_score=0.0),
        ],
    )
    assert len(results.completed) == 2
    avg = sum(t.automated_score for t in results.completed) / len(results.completed)
    assert avg == 0.5


def test_experiment():
    exp = Experiment(
        name="test-exp",
        configs=[
            ExperimentConfig(label="baseline"),
            ExperimentConfig(label="with-mcp", mcp_config={"tools": ["search"]}),
        ],
    )
    assert len(exp.configs) == 2
    assert exp.configs[1].label == "with-mcp"
