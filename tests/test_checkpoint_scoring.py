"""Tests for checkpoint-weighted scoring from task.toml [[checkpoints]]."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from codeprobe.core.scoring import CheckpointScorer, ScoreResult
from codeprobe.models.task import Checkpoint, TaskVerification

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_verifier(task_dir: Path, name: str, script: str) -> None:
    """Write a verifier script into task_dir/tests/verifiers/."""
    verifiers_dir = task_dir / "tests" / "verifiers"
    verifiers_dir.mkdir(parents=True, exist_ok=True)
    verifier = verifiers_dir / name
    verifier.write_text(script)
    verifier.chmod(verifier.stat().st_mode | stat.S_IEXEC)


def _write_checkpoints_json(
    task_dir: Path, checkpoints: list[dict[str, object]]
) -> None:
    """Write tests/checkpoints.json."""
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (tests_dir / "checkpoints.json").write_text(
        json.dumps(checkpoints), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Checkpoint dataclass
# ---------------------------------------------------------------------------


class TestCheckpointDataclass:
    def test_frozen(self) -> None:
        cp = Checkpoint(name="a", weight=0.5, verifier="a.sh")
        with pytest.raises(AttributeError):
            cp.name = "b"  # type: ignore[misc]

    def test_defaults(self) -> None:
        cp = Checkpoint(name="a", weight=0.5, verifier="a.sh")
        assert cp.description == ""

    def test_all_fields(self) -> None:
        cp = Checkpoint(
            name="syntax", weight=0.3, verifier="syntax.sh", description="Checks syntax"
        )
        assert cp.name == "syntax"
        assert cp.weight == 0.3
        assert cp.verifier == "syntax.sh"
        assert cp.description == "Checks syntax"


# ---------------------------------------------------------------------------
# TaskVerification.checkpoints field
# ---------------------------------------------------------------------------


class TestTaskVerificationCheckpoints:
    def test_default_empty(self) -> None:
        tv = TaskVerification()
        assert tv.checkpoints == ()

    def test_stores_checkpoints(self) -> None:
        cps = (
            Checkpoint(name="a", weight=0.6, verifier="a.sh"),
            Checkpoint(name="b", weight=0.4, verifier="b.sh"),
        )
        tv = TaskVerification(checkpoints=cps)
        assert len(tv.checkpoints) == 2
        assert tv.checkpoints[0].name == "a"


# ---------------------------------------------------------------------------
# CheckpointScorer — metadata checkpoints (new path)
# ---------------------------------------------------------------------------


class TestMetadataCheckpoints:
    """CheckpointScorer initialised with metadata_checkpoints (from task.toml)."""

    def test_three_checkpoints_one_fails_score_055(self, tmp_path: Path) -> None:
        """Core acceptance test: weights 0.2, 0.45, 0.35 with cp2 failing -> 0.55."""
        task_dir = tmp_path / "task"
        _make_verifier(task_dir, "cp1.sh", "#!/bin/bash\nexit 0\n")
        _make_verifier(task_dir, "cp2.sh", "#!/bin/bash\nexit 1\n")  # fails
        _make_verifier(task_dir, "cp3.sh", "#!/bin/bash\nexit 0\n")

        metadata_cps = [
            {"name": "cp1", "weight": 0.2, "verifier": "cp1.sh"},
            {"name": "cp2", "weight": 0.45, "verifier": "cp2.sh"},
            {"name": "cp3", "weight": 0.35, "verifier": "cp3.sh"},
        ]

        scorer = CheckpointScorer(metadata_checkpoints=metadata_cps)
        result = scorer.score("", task_dir)

        assert result.score == pytest.approx(0.55)
        assert result.passed is True
        assert result.error is None

    def test_all_pass(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "task"
        _make_verifier(task_dir, "a.sh", "#!/bin/bash\nexit 0\n")
        _make_verifier(task_dir, "b.sh", "#!/bin/bash\nexit 0\n")

        scorer = CheckpointScorer(
            metadata_checkpoints=[
                {"name": "a", "weight": 0.6, "verifier": "a.sh"},
                {"name": "b", "weight": 0.4, "verifier": "b.sh"},
            ]
        )
        result = scorer.score("", task_dir)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True

    def test_all_fail(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "task"
        _make_verifier(task_dir, "a.sh", "#!/bin/bash\nexit 1\n")
        _make_verifier(task_dir, "b.sh", "#!/bin/bash\nexit 1\n")

        scorer = CheckpointScorer(
            metadata_checkpoints=[
                {"name": "a", "weight": 0.6, "verifier": "a.sh"},
                {"name": "b", "weight": 0.4, "verifier": "b.sh"},
            ]
        )
        result = scorer.score("", task_dir)
        assert result.score == 0.0
        assert result.passed is False

    def test_json_output_verifier(self, tmp_path: Path) -> None:
        """Verifier that emits JSON score (partial credit)."""
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
        # 0.5 * 0.6 + 1.0 * 0.4 = 0.7
        assert result.score == pytest.approx(0.7)

    def test_weights_must_sum_to_one(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "task"
        _make_verifier(task_dir, "a.sh", "#!/bin/bash\nexit 0\n")

        scorer = CheckpointScorer(
            metadata_checkpoints=[
                {"name": "a", "weight": 0.3, "verifier": "a.sh"},
            ]
        )
        result = scorer.score("", task_dir)
        assert result.passed is False
        assert "weights must sum to 1.0" in result.error

    def test_missing_verifier_file(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "task"
        (task_dir / "tests" / "verifiers").mkdir(parents=True)

        scorer = CheckpointScorer(
            metadata_checkpoints=[
                {"name": "a", "weight": 1.0, "verifier": "missing.sh"},
            ]
        )
        result = scorer.score("", task_dir)
        assert result.passed is False
        assert "Verifier not found" in result.error

    def test_metadata_takes_precedence_over_json(self, tmp_path: Path) -> None:
        """When metadata_checkpoints provided, checkpoints.json is ignored."""
        task_dir = tmp_path / "task"
        _make_verifier(task_dir, "meta.sh", "#!/bin/bash\nexit 0\n")

        # Write a checkpoints.json that would fail (bad weights)
        _write_checkpoints_json(
            task_dir,
            [{"name": "json_cp", "weight": 0.99, "verifier": "nonexistent.sh"}],
        )

        scorer = CheckpointScorer(
            metadata_checkpoints=[
                {"name": "meta_cp", "weight": 1.0, "verifier": "meta.sh"},
            ]
        )
        result = scorer.score("", task_dir)
        assert result.score == pytest.approx(1.0)
        assert result.passed is True


# ---------------------------------------------------------------------------
# CheckpointScorer — legacy checkpoints.json fallback
# ---------------------------------------------------------------------------


class TestCheckpointsJsonFallback:
    """CheckpointScorer with no metadata falls back to checkpoints.json."""

    def test_reads_checkpoints_json(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "task"
        _make_verifier(task_dir, "a.sh", "#!/bin/bash\nexit 0\n")
        _make_verifier(task_dir, "b.sh", "#!/bin/bash\nexit 1\n")

        _write_checkpoints_json(
            task_dir,
            [
                {"name": "a", "weight": 0.7, "verifier": "a.sh"},
                {"name": "b", "weight": 0.3, "verifier": "b.sh"},
            ],
        )

        scorer = CheckpointScorer()  # no metadata
        result = scorer.score("", task_dir)
        assert result.score == pytest.approx(0.7)
        assert result.passed is True

    def test_missing_checkpoints_json(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "task"
        (task_dir / "tests").mkdir(parents=True)

        scorer = CheckpointScorer()
        result = scorer.score("", task_dir)
        assert result.passed is False
        assert "checkpoints.json not found" in result.error

    def test_invalid_checkpoints_json(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "task"
        tests_dir = task_dir / "tests"
        tests_dir.mkdir(parents=True)
        (tests_dir / "checkpoints.json").write_text("not json")

        scorer = CheckpointScorer()
        result = scorer.score("", task_dir)
        assert result.passed is False
        assert "Invalid checkpoints.json" in result.error


# ---------------------------------------------------------------------------
# TOML loader integration
# ---------------------------------------------------------------------------


class TestTomlCheckpointLoading:
    """Loading [[checkpoints]] from task.toml via the loader."""

    def test_load_task_with_checkpoints(self, tmp_path: Path) -> None:
        from codeprobe.loaders import load_task

        toml_content = """\
[task]
id = "test-task-001"
repo = "example/repo"

[verification]
reward_type = "checkpoint"

[[checkpoints]]
name = "syntax"
weight = 0.2
verifier = "syntax.sh"
description = "Check syntax"

[[checkpoints]]
name = "logic"
weight = 0.45
verifier = "logic.sh"
description = "Check logic"

[[checkpoints]]
name = "style"
weight = 0.35
verifier = "style.sh"
"""
        toml_file = tmp_path / "task.toml"
        toml_file.write_text(toml_content)

        task = load_task(toml_file)
        assert len(task.verification.checkpoints) == 3
        assert task.verification.checkpoints[0].name == "syntax"
        assert task.verification.checkpoints[0].weight == 0.2
        assert task.verification.checkpoints[0].verifier == "syntax.sh"
        assert task.verification.checkpoints[0].description == "Check syntax"
        assert task.verification.checkpoints[1].weight == 0.45
        assert task.verification.checkpoints[2].weight == 0.35

    def test_load_task_without_checkpoints(self, tmp_path: Path) -> None:
        from codeprobe.loaders import load_task

        toml_content = """\
[task]
id = "test-task-002"
repo = "example/repo"

[verification]
reward_type = "binary"
"""
        toml_file = tmp_path / "task.toml"
        toml_file.write_text(toml_content)

        task = load_task(toml_file)
        assert task.verification.checkpoints == ()

    def test_end_to_end_toml_to_scorer(self, tmp_path: Path) -> None:
        """Load checkpoints from TOML, feed to scorer, verify score."""
        from codeprobe.loaders import load_task

        toml_content = """\
[task]
id = "e2e-test"
repo = "example/repo"

[verification]
reward_type = "checkpoint"

[[checkpoints]]
name = "cp1"
weight = 0.2
verifier = "cp1.sh"

[[checkpoints]]
name = "cp2"
weight = 0.45
verifier = "cp2.sh"

[[checkpoints]]
name = "cp3"
weight = 0.35
verifier = "cp3.sh"
"""
        toml_file = tmp_path / "task.toml"
        toml_file.write_text(toml_content)

        task = load_task(toml_file)

        # Set up verifiers in a task directory
        task_dir = tmp_path / "task_dir"
        _make_verifier(task_dir, "cp1.sh", "#!/bin/bash\nexit 0\n")
        _make_verifier(task_dir, "cp2.sh", "#!/bin/bash\nexit 1\n")  # fails
        _make_verifier(task_dir, "cp3.sh", "#!/bin/bash\nexit 0\n")

        # Convert Checkpoint dataclasses to dicts for the scorer
        metadata_cps = [
            {"name": cp.name, "weight": cp.weight, "verifier": cp.verifier}
            for cp in task.verification.checkpoints
        ]
        scorer = CheckpointScorer(metadata_checkpoints=metadata_cps)
        result = scorer.score("", task_dir)

        # 0.2 * 1.0 + 0.45 * 0.0 + 0.35 * 1.0 = 0.55
        assert result.score == pytest.approx(0.55)
        assert result.passed is True
