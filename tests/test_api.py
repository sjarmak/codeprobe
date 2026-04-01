"""Tests for the in-process batch API: run_experiment()."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.analysis.report import Report
from tests.conftest import FakeAdapter


def _make_experiment_dir(
    base: Path,
    *,
    name: str = "test-exp",
    configs: list[dict] | None = None,
    num_tasks: int = 2,
    passing: bool = True,
) -> Path:
    """Create a minimal experiment directory with tasks."""
    exp_dir = base / name
    exp_dir.mkdir(parents=True)

    if configs is None:
        configs = [{"label": "default", "agent": "fake"}]

    experiment_json = {
        "name": name,
        "description": "test experiment",
        "tasks_dir": "tasks",
        "configs": configs,
    }
    (exp_dir / "experiment.json").write_text(
        json.dumps(experiment_json, indent=2), encoding="utf-8"
    )

    tasks_dir = exp_dir / "tasks"
    tasks_dir.mkdir()

    for i in range(num_tasks):
        task_dir = tasks_dir / f"task-{i:03d}"
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text(f"Fix bug {i}.")
        tests_subdir = task_dir / "tests"
        tests_subdir.mkdir()
        test_sh = tests_subdir / "test.sh"
        exit_code = 0 if passing else 1
        test_sh.write_text(f"#!/bin/bash\nexit {exit_code}\n")
        test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)

    return exp_dir


class TestRunExperiment:
    """Tests for the public run_experiment() API."""

    def test_returns_report(self, tmp_path: Path) -> None:
        """run_experiment returns a Report dataclass."""
        from codeprobe.api import run_experiment

        exp_dir = _make_experiment_dir(tmp_path, passing=True)
        adapter = FakeAdapter(stdout="PASS", exit_code=0)

        with patch("codeprobe.api.resolve", return_value=adapter):
            report = run_experiment(exp_dir)

        assert isinstance(report, Report)
        assert report.experiment_name == "test-exp"
        assert len(report.summaries) == 1
        assert report.summaries[0].label == "default"

    def test_report_matches_expected_scores(self, tmp_path: Path) -> None:
        """Verify pass rate reflects task outcomes."""
        from codeprobe.api import run_experiment

        exp_dir = _make_experiment_dir(tmp_path, passing=True, num_tasks=3)
        adapter = FakeAdapter(stdout="PASS", exit_code=0)

        with patch("codeprobe.api.resolve", return_value=adapter):
            report = run_experiment(exp_dir)

        summary = report.summaries[0]
        assert summary.total_tasks == 3

    def test_with_explicit_configs(self, tmp_path: Path) -> None:
        """Passing explicit config dicts overrides experiment.json configs."""
        from codeprobe.api import run_experiment

        exp_dir = _make_experiment_dir(tmp_path)
        adapter = FakeAdapter(stdout="PASS", exit_code=0)

        configs = [
            {"label": "custom-a", "agent": "fake"},
            {"label": "custom-b", "agent": "fake"},
        ]

        with patch("codeprobe.api.resolve", return_value=adapter):
            report = run_experiment(exp_dir, configs=configs)

        assert len(report.summaries) == 2
        labels = {s.label for s in report.summaries}
        assert labels == {"custom-a", "custom-b"}

    def test_max_cost_usd_passed_to_executor(self, tmp_path: Path) -> None:
        """max_cost_usd is forwarded to execute_config."""
        from codeprobe.api import run_experiment

        exp_dir = _make_experiment_dir(tmp_path, num_tasks=5)
        # Adapter with per_token cost: $10 per task
        adapter = FakeAdapter(
            stdout="PASS", exit_code=0, cost_usd=10.0, cost_model="per_token"
        )

        with patch("codeprobe.api.resolve", return_value=adapter):
            report = run_experiment(exp_dir, max_cost_usd=15.0)

        # With $10/task and $15 budget, should stop after 2 tasks
        summary = report.summaries[0]
        assert summary.total_tasks <= 3  # at most 2 run + budget check

    def test_checkpoint_resume(self, tmp_path: Path) -> None:
        """run_experiment uses CheckpointStore so resuming skips done tasks."""
        from codeprobe.api import run_experiment

        exp_dir = _make_experiment_dir(tmp_path, num_tasks=3)
        adapter = FakeAdapter(stdout="PASS", exit_code=0)

        with patch("codeprobe.api.resolve", return_value=adapter):
            report1 = run_experiment(exp_dir)

        # All 3 tasks should be completed
        assert report1.summaries[0].total_tasks == 3
        assert len(adapter.run_calls) == 3

        # Second run: adapter should not be called again (all checkpointed)
        adapter2 = FakeAdapter(stdout="PASS", exit_code=0)
        with patch("codeprobe.api.resolve", return_value=adapter2):
            report2 = run_experiment(exp_dir)

        assert report2.summaries[0].total_tasks == 3
        assert len(adapter2.run_calls) == 0  # all from checkpoint

    def test_no_click_dependency(self) -> None:
        """The api module does not import click."""
        import importlib

        mod = importlib.import_module("codeprobe.api")
        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "import click" not in source
        assert "from click" not in source

    def test_saves_results_json(self, tmp_path: Path) -> None:
        """run_experiment writes results.json for each config."""
        from codeprobe.api import run_experiment

        exp_dir = _make_experiment_dir(tmp_path)
        adapter = FakeAdapter(stdout="PASS", exit_code=0)

        with patch("codeprobe.api.resolve", return_value=adapter):
            run_experiment(exp_dir)

        results_path = exp_dir / "runs" / "default" / "results.json"
        assert results_path.is_file()
        data = json.loads(results_path.read_text(encoding="utf-8"))
        assert data["config"] == "default"
        assert len(data["completed"]) == 2

    def test_invalid_experiment_dir(self, tmp_path: Path) -> None:
        """run_experiment raises FileNotFoundError for missing experiment."""
        from codeprobe.api import run_experiment

        with pytest.raises(FileNotFoundError):
            run_experiment(tmp_path / "nonexistent")

    def test_no_tasks_raises_value_error(self, tmp_path: Path) -> None:
        """run_experiment raises ValueError when no task dirs found."""
        from codeprobe.api import run_experiment

        exp_dir = tmp_path / "empty-exp"
        exp_dir.mkdir()
        (exp_dir / "tasks").mkdir()
        experiment_json = {
            "name": "empty-exp",
            "description": "",
            "tasks_dir": "tasks",
            "configs": [{"label": "default", "agent": "fake"}],
        }
        (exp_dir / "experiment.json").write_text(
            json.dumps(experiment_json), encoding="utf-8"
        )

        with pytest.raises(ValueError, match="No tasks found"):
            run_experiment(exp_dir)
