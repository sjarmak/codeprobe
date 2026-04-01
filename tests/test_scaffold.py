"""Tests for scaffold writer and CLI commands."""

from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.scaffold.writer import (
    TaskSpec,
    ValidationError,
    validate_task_dir,
    write_task_dir,
)

# ---------------------------------------------------------------------------
# TaskSpec frozen dataclass
# ---------------------------------------------------------------------------


class TestTaskSpec:
    def test_frozen(self) -> None:
        spec = TaskSpec(task_id="my-task", repo="owner/repo")
        with pytest.raises(AttributeError):
            spec.task_id = "other"  # type: ignore[misc]

    def test_defaults(self) -> None:
        spec = TaskSpec(task_id="t1", repo="r")
        assert spec.difficulty == "medium"
        assert spec.category == "sdlc"
        assert spec.description == ""
        assert spec.time_limit_sec == 300
        assert spec.instruction == ""
        assert spec.reward_type == "binary"
        assert spec.tags == ()

    def test_duration_and_resource_defaults(self) -> None:
        spec = TaskSpec(task_id="t1", repo="r")
        assert spec.estimated_duration_sec == 300
        assert spec.resource_tier == "medium"

    def test_custom_values(self) -> None:
        spec = TaskSpec(
            task_id="hard-task",
            repo="org/repo",
            difficulty="hard",
            category="debug",
            description="Fix the bug",
            time_limit_sec=600,
            instruction="Do the thing.",
            reward_type="test_ratio",
            tags=("python", "security"),
            estimated_duration_sec=900,
            resource_tier="heavy",
        )
        assert spec.task_id == "hard-task"
        assert spec.tags == ("python", "security")
        assert spec.estimated_duration_sec == 900
        assert spec.resource_tier == "heavy"


# ---------------------------------------------------------------------------
# write_task_dir
# ---------------------------------------------------------------------------


class TestWriteTaskDir:
    def test_creates_directory_structure(self, tmp_path: Path) -> None:
        spec = TaskSpec(
            task_id="fix-auth",
            repo="acme/app",
            instruction="Fix the auth bug.",
            description="Authentication bypass",
        )
        result = write_task_dir(spec, tmp_path)

        assert result == tmp_path / "fix-auth"
        assert (result / "instruction.md").is_file()
        assert (result / "tests" / "test.sh").is_file()
        assert (result / "task.toml").is_file()

    def test_instruction_md_content(self, tmp_path: Path) -> None:
        spec = TaskSpec(
            task_id="t1",
            repo="r",
            instruction="Do the thing.",
        )
        result = write_task_dir(spec, tmp_path)
        content = (result / "instruction.md").read_text(encoding="utf-8")
        assert "Do the thing." in content

    def test_task_toml_is_valid_toml(self, tmp_path: Path) -> None:
        spec = TaskSpec(
            task_id="toml-task",
            repo="org/repo",
            difficulty="hard",
            category="debug",
            description="A task",
            time_limit_sec=600,
            reward_type="test_ratio",
            tags=("python",),
        )
        result = write_task_dir(spec, tmp_path)
        with (result / "task.toml").open("rb") as f:
            data = tomllib.load(f)

        assert data["task"]["id"] == "toml-task"
        assert data["task"]["repo"] == "org/repo"
        assert data["task"]["time_limit_sec"] == 600
        assert data["metadata"]["difficulty"] == "hard"
        assert data["metadata"]["category"] == "debug"
        assert data["metadata"]["description"] == "A task"
        assert data["verification"]["reward_type"] == "test_ratio"

    def test_task_toml_includes_duration_and_resource(self, tmp_path: Path) -> None:
        spec = TaskSpec(
            task_id="dur-task",
            repo="org/repo",
            estimated_duration_sec=600,
            resource_tier="heavy",
        )
        result = write_task_dir(spec, tmp_path)
        with (result / "task.toml").open("rb") as f:
            data = tomllib.load(f)

        assert data["metadata"]["estimated_duration_sec"] == 600
        assert data["metadata"]["resource_tier"] == "heavy"

    def test_task_toml_default_duration_and_resource(self, tmp_path: Path) -> None:
        spec = TaskSpec(task_id="def-task", repo="org/repo")
        result = write_task_dir(spec, tmp_path)
        with (result / "task.toml").open("rb") as f:
            data = tomllib.load(f)

        assert data["metadata"]["estimated_duration_sec"] == 300
        assert data["metadata"]["resource_tier"] == "medium"

    def test_test_sh_is_executable(self, tmp_path: Path) -> None:
        spec = TaskSpec(task_id="t1", repo="r")
        result = write_task_dir(spec, tmp_path)
        test_sh = result / "tests" / "test.sh"
        mode = test_sh.stat().st_mode
        assert mode & stat.S_IXUSR, "test.sh should be user-executable"

    def test_test_sh_has_shebang(self, tmp_path: Path) -> None:
        spec = TaskSpec(task_id="t1", repo="r")
        result = write_task_dir(spec, tmp_path)
        content = (result / "tests" / "test.sh").read_text(encoding="utf-8")
        assert content.startswith("#!/usr/bin/env bash")

    def test_rejects_path_traversal_id(self, tmp_path: Path) -> None:
        spec = TaskSpec(task_id="../escape", repo="r")
        with pytest.raises(ValueError, match="Invalid task id"):
            write_task_dir(spec, tmp_path)

    def test_rejects_empty_id(self, tmp_path: Path) -> None:
        spec = TaskSpec(task_id="", repo="r")
        with pytest.raises(ValueError, match="Invalid task id"):
            write_task_dir(spec, tmp_path)

    def test_idempotent_overwrites(self, tmp_path: Path) -> None:
        spec = TaskSpec(task_id="t1", repo="r", instruction="v1")
        write_task_dir(spec, tmp_path)
        spec2 = TaskSpec(task_id="t1", repo="r", instruction="v2")
        result = write_task_dir(spec2, tmp_path)
        content = (result / "instruction.md").read_text(encoding="utf-8")
        assert "v2" in content


# ---------------------------------------------------------------------------
# validate_task_dir
# ---------------------------------------------------------------------------


class TestValidateTaskDir:
    def _make_valid_task(self, tmp_path: Path) -> Path:
        spec = TaskSpec(
            task_id="valid-task",
            repo="org/repo",
            instruction="Do the thing.",
            description="A valid task",
        )
        return write_task_dir(spec, tmp_path)

    def test_valid_dir_passes(self, tmp_path: Path) -> None:
        task_dir = self._make_valid_task(tmp_path)
        errors = validate_task_dir(task_dir)
        assert errors == []

    def test_missing_instruction(self, tmp_path: Path) -> None:
        task_dir = self._make_valid_task(tmp_path)
        (task_dir / "instruction.md").unlink()
        errors = validate_task_dir(task_dir)
        assert any("instruction.md" in e.message for e in errors)

    def test_missing_test_sh(self, tmp_path: Path) -> None:
        task_dir = self._make_valid_task(tmp_path)
        (task_dir / "tests" / "test.sh").unlink()
        errors = validate_task_dir(task_dir)
        assert any("test.sh" in e.message for e in errors)

    def test_missing_task_toml(self, tmp_path: Path) -> None:
        task_dir = self._make_valid_task(tmp_path)
        (task_dir / "task.toml").unlink()
        errors = validate_task_dir(task_dir)
        assert any("task.toml" in e.message for e in errors)

    def test_nonexistent_dir(self, tmp_path: Path) -> None:
        errors = validate_task_dir(tmp_path / "nope")
        assert any("does not exist" in e.message for e in errors)

    def test_test_sh_not_executable(self, tmp_path: Path) -> None:
        task_dir = self._make_valid_task(tmp_path)
        test_sh = task_dir / "tests" / "test.sh"
        test_sh.chmod(0o644)
        errors = validate_task_dir(task_dir)
        assert any("executable" in e.message for e in errors)


# ---------------------------------------------------------------------------
# CLI: codeprobe scaffold task
# ---------------------------------------------------------------------------


class TestScaffoldTaskCLI:
    def test_scaffold_command_registered(self) -> None:
        runner = CliRunner()
        result = runner.invoke(main, ["scaffold", "--help"])
        assert result.exit_code == 0
        assert "task" in result.output
        assert "validate" in result.output

    def test_scaffold_task_creates_dir(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "scaffold",
                "task",
                "--id",
                "my-task",
                "--repo",
                "org/repo",
                "--output",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / "my-task" / "instruction.md").is_file()
        assert (tmp_path / "my-task" / "task.toml").is_file()
        assert (tmp_path / "my-task" / "tests" / "test.sh").is_file()

    def test_scaffold_task_with_instruction(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "scaffold",
                "task",
                "--id",
                "instr-task",
                "--repo",
                "org/repo",
                "--instruction",
                "Fix the auth bug.",
                "--output",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        content = (tmp_path / "instr-task" / "instruction.md").read_text()
        assert "Fix the auth bug." in content

    def test_scaffold_task_missing_id(self, tmp_path: Path) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["scaffold", "task", "--repo", "org/repo", "--output", str(tmp_path)],
        )
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# CLI: codeprobe scaffold validate
# ---------------------------------------------------------------------------


class TestScaffoldValidateCLI:
    def test_validate_valid_dir(self, tmp_path: Path) -> None:
        # Create a valid task first
        runner = CliRunner()
        runner.invoke(
            main,
            [
                "scaffold",
                "task",
                "--id",
                "v-task",
                "--repo",
                "org/repo",
                "--output",
                str(tmp_path),
            ],
        )
        result = runner.invoke(
            main,
            ["scaffold", "validate", str(tmp_path / "v-task")],
        )
        assert result.exit_code == 0
        assert "valid" in result.output.lower() or "pass" in result.output.lower()

    def test_validate_invalid_dir(self, tmp_path: Path) -> None:
        # Create empty dir
        task_dir = tmp_path / "bad-task"
        task_dir.mkdir()
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["scaffold", "validate", str(task_dir)],
        )
        assert result.exit_code == 1
