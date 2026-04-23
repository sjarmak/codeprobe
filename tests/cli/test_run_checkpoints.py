"""Tests for R17 per-checkpoint score propagation through the run pipeline.

Covers:
- ``CheckpointScorer`` surfaces a ``checkpoint_scores`` dict in
  ``ScoreResult.details`` so downstream scoring.json carries the per-step
  breakdown.
- ``_save_task_artifacts`` writes ``scoring.json`` with the
  ``checkpoint_scores`` map.
- ``format_csv_report`` includes a ``checkpoint_scores`` column populated
  with JSON for checkpoint tasks and an empty string otherwise.
- ``format_json_report`` preserves the native ``checkpoint_scores`` dict.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from codeprobe.analysis.report import (
    Report,
    format_csv_report,
    format_json_report,
    generate_report,
)
from codeprobe.core.scoring import CheckpointScorer
from codeprobe.models.experiment import CompletedTask, ConfigResults


def _make_verifier(task_dir: Path, name: str, script: str) -> None:
    verifiers_dir = task_dir / "tests" / "verifiers"
    verifiers_dir.mkdir(parents=True, exist_ok=True)
    verifier = verifiers_dir / name
    verifier.write_text(script)
    verifier.chmod(verifier.stat().st_mode | stat.S_IEXEC)


class TestCheckpointScorerEmitsDetails:
    """The scorer populates ``details['checkpoint_scores']`` per step."""

    def test_per_checkpoint_scores_in_details(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "task"
        _make_verifier(task_dir, "step1.sh", "#!/bin/bash\nexit 0\n")  # pass
        _make_verifier(task_dir, "step2.sh", "#!/bin/bash\nexit 1\n")  # fail

        scorer = CheckpointScorer(
            metadata_checkpoints=[
                {"name": "step1", "weight": 0.4, "verifier": "step1.sh"},
                {"name": "step2", "weight": 0.6, "verifier": "step2.sh"},
            ]
        )
        result = scorer.score("", task_dir)

        assert result.details
        cp_scores = result.details.get("checkpoint_scores")
        assert isinstance(cp_scores, dict)
        assert cp_scores["step1"] == pytest.approx(1.0)
        assert cp_scores["step2"] == pytest.approx(0.0)
        # Composite score is weighted sum.
        assert result.score == pytest.approx(0.4)

    def test_weights_also_recorded(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "task"
        _make_verifier(task_dir, "a.sh", "#!/bin/bash\nexit 0\n")
        _make_verifier(task_dir, "b.sh", "#!/bin/bash\nexit 0\n")
        scorer = CheckpointScorer(
            metadata_checkpoints=[
                {"name": "a", "weight": 0.25, "verifier": "a.sh"},
                {"name": "b", "weight": 0.75, "verifier": "b.sh"},
            ]
        )
        result = scorer.score("", task_dir)
        weights = result.details.get("checkpoint_weights")
        assert weights == {"a": 0.25, "b": 0.75}

    def test_partial_credit_via_json_stdout(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "task"
        _make_verifier(
            task_dir,
            "partial.sh",
            '#!/bin/bash\necho \'{"score": 0.5, "passed": true}\'\nexit 0\n',
        )
        _make_verifier(task_dir, "pass.sh", "#!/bin/bash\nexit 0\n")

        scorer = CheckpointScorer(
            metadata_checkpoints=[
                {"name": "partial", "weight": 0.6, "verifier": "partial.sh"},
                {"name": "pass", "weight": 0.4, "verifier": "pass.sh"},
            ]
        )
        result = scorer.score("", task_dir)
        cp_scores = result.details["checkpoint_scores"]
        assert cp_scores["partial"] == pytest.approx(0.5)
        assert cp_scores["pass"] == pytest.approx(1.0)


class TestScoringJsonCarriesCheckpointScores:
    """``_save_task_artifacts`` writes ``scoring.json`` with the map."""

    def test_scoring_json_contains_checkpoint_scores(self, tmp_path: Path) -> None:
        from codeprobe.core.executor import TaskResult, _save_task_artifacts

        runs_dir = tmp_path / "runs"
        completed = CompletedTask(
            task_id="t1",
            automated_score=0.4,
            status="completed",
            scoring_details={
                "passed": False,
                "error": None,
                "checkpoint_scores": {"step1": 1.0, "step2": 0.0},
                "checkpoint_weights": {"step1": 0.4, "step2": 0.6},
            },
        )
        task_result = TaskResult(
            completed=completed,
            agent_stdout="",
            agent_stderr="",
        )
        _save_task_artifacts(runs_dir, "t1", task_result)

        scoring_path = runs_dir / "t1" / "scoring.json"
        assert scoring_path.is_file()
        data = json.loads(scoring_path.read_text())
        assert data["score"] == pytest.approx(0.4)
        assert data["checkpoint_scores"] == {"step1": 1.0, "step2": 0.0}
        assert data["checkpoint_weights"] == {"step1": 0.4, "step2": 0.6}


def _build_tiny_report(*, with_checkpoints: bool) -> Report:
    """Build a Report with one config and one task; optionally carrying checkpoint_scores."""
    details: dict = {"passed": True, "error": None}
    if with_checkpoints:
        details["checkpoint_scores"] = {"step1": 1.0, "step2": 0.5}
        details["checkpoint_weights"] = {"step1": 0.4, "step2": 0.6}
    task = CompletedTask(
        task_id="taskA",
        automated_score=0.7,
        status="completed",
        duration_seconds=1.0,
        scoring_details=details,
    )
    config_result = ConfigResults(
        config="cfgA",
        completed=[task],
    )
    return generate_report("demo", [config_result])


class TestInterpretSurfacesCheckpointScores:
    """Interpret output includes per-checkpoint partial-credit breakdown."""

    def test_csv_has_checkpoint_scores_column(self) -> None:
        report = _build_tiny_report(with_checkpoints=True)
        csv_text = format_csv_report(report)
        lines = [ln for ln in csv_text.splitlines() if not ln.startswith("#")]
        header = lines[0].split(",")
        assert "checkpoint_scores" in header

        # Row includes the JSON-encoded dict.
        data_row = lines[1]
        assert "step1" in data_row
        assert "step2" in data_row

    def test_csv_empty_for_non_checkpoint_tasks(self) -> None:
        report = _build_tiny_report(with_checkpoints=False)
        csv_text = format_csv_report(report)
        lines = [ln for ln in csv_text.splitlines() if not ln.startswith("#")]
        header = lines[0].split(",")
        # Column must still exist so the CSV schema is uniform.
        assert "checkpoint_scores" in header
        cp_idx = header.index("checkpoint_scores")
        row = lines[1]
        import csv as _csv

        parsed = next(_csv.reader([row]))
        assert parsed[cp_idx] == ""

    def test_json_preserves_checkpoint_scores_dict(self) -> None:
        report = _build_tiny_report(with_checkpoints=True)
        data = json.loads(format_json_report(report))
        tasks = data["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["checkpoint_scores"] == {"step1": 1.0, "step2": 0.5}
        # The CSV-helper field is stripped from the JSON view.
        assert "checkpoint_scores_csv" not in tasks[0]
