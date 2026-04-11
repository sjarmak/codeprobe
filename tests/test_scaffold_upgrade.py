"""Tests for scaffold upgrade-to-dual functionality."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.scaffold.writer import upgrade_to_dual


def _write_metadata(task_dir: Path, meta: dict) -> Path:
    """Helper to write metadata.json into a task directory."""
    meta_path = task_dir / "metadata.json"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    return meta_path


def _make_task_dir(
    tmp_path: Path, verification_mode: str = "test_script", commit: str = "abc1234"
) -> Path:
    """Create a minimal task directory with metadata.json and tests/test.sh."""
    task_dir = tmp_path / "my-task"
    task_dir.mkdir()
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test.sh").write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")

    meta: dict = {
        "task_id": "my-task",
        "repo": "org/repo",
        "verification": {
            "verification_mode": verification_mode,
            "reward_type": "binary",
        },
    }
    if commit is not None:
        meta["verification"]["ground_truth_commit"] = commit
    _write_metadata(task_dir, meta)
    return task_dir


class TestUpgradeToDualHappyPath:
    """Happy path: test_script mode with valid commit creates ground_truth.json and updates metadata."""

    def test_creates_ground_truth_json(self, tmp_path: Path) -> None:
        task_dir = _make_task_dir(tmp_path, commit="a" * 40)
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        fake_diff_output = "src/foo.py\nsrc/bar.py\ntests/test_baz.py\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=fake_diff_output
            )
            result = upgrade_to_dual(task_dir, repo_path)

        assert result == task_dir

        gt_path = task_dir / "tests" / "ground_truth.json"
        assert gt_path.is_file()
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        assert gt["answer_type"] == "file_list"
        # test files filtered out
        assert gt["answer"] == ["src/bar.py", "src/foo.py"]
        assert gt["oracle_metadata"]["source"] == "upgrade-to-dual"
        assert gt["oracle_metadata"]["ground_truth_commit"] == "a" * 40

    def test_updates_metadata_to_dual(self, tmp_path: Path) -> None:
        task_dir = _make_task_dir(tmp_path, commit="b" * 40)
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        fake_diff_output = "src/main.py\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=fake_diff_output
            )
            upgrade_to_dual(task_dir, repo_path)

        meta = json.loads((task_dir / "metadata.json").read_text(encoding="utf-8"))
        assert meta["verification"]["verification_mode"] == "dual"


class TestUpgradeToDualSkip:
    """Already-dual task dirs should be skipped."""

    def test_already_dual_returns_early(self, tmp_path: Path) -> None:
        task_dir = _make_task_dir(tmp_path, verification_mode="dual", commit="c" * 40)
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        result = upgrade_to_dual(task_dir, repo_path)
        assert result == task_dir
        # ground_truth.json NOT created (no-op)
        assert not (task_dir / "tests" / "ground_truth.json").is_file()


class TestUpgradeToDualErrors:
    """Error cases."""

    def test_missing_metadata_raises(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "no-meta"
        task_dir.mkdir()

        with pytest.raises(ValueError, match="metadata.json"):
            upgrade_to_dual(task_dir, tmp_path)

    def test_missing_ground_truth_commit_raises(self, tmp_path: Path) -> None:
        task_dir = _make_task_dir(tmp_path, commit=None)
        # Remove ground_truth_commit key
        meta = json.loads((task_dir / "metadata.json").read_text(encoding="utf-8"))
        assert "ground_truth_commit" not in meta["verification"]

        with pytest.raises(ValueError, match="ground_truth_commit"):
            upgrade_to_dual(task_dir, tmp_path)

    def test_invalid_sha_raises(self, tmp_path: Path) -> None:
        task_dir = _make_task_dir(tmp_path, commit="not-a-sha!!")

        with pytest.raises(ValueError, match="[Ii]nvalid.*SHA"):
            upgrade_to_dual(task_dir, tmp_path)

    def test_empty_diff_raises(self, tmp_path: Path) -> None:
        task_dir = _make_task_dir(tmp_path, commit="d" * 40)
        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        # All files are test files
        fake_diff_output = "tests/test_foo.py\ntest_bar.py\n"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=fake_diff_output
            )
            with pytest.raises(ValueError, match="[Nn]o non-test"):
                upgrade_to_dual(task_dir, repo_path)
