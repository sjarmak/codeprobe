"""Tests for suite.toml manifest loading and task filtering."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from codeprobe.loaders.suite import load_suite
from codeprobe.models.suite import Suite

# ---------------------------------------------------------------------------
# Suite dataclass
# ---------------------------------------------------------------------------


class TestSuiteDataclass:
    def test_frozen(self) -> None:
        suite = Suite(name="test")
        with pytest.raises(AttributeError):
            suite.name = "changed"  # type: ignore[misc]

    def test_defaults(self) -> None:
        suite = Suite(name="basic")
        assert suite.name == "basic"
        assert suite.description == ""
        assert suite.task_dir == "tasks"
        assert suite.task_types == ()
        assert suite.difficulties == ()
        assert suite.eval_goal == ""
        assert suite.tags == ()
        assert suite.task_ids == ()

    def test_all_fields(self) -> None:
        suite = Suite(
            name="full",
            description="A full suite",
            task_dir="my_tasks",
            task_types=("micro_probe",),
            difficulties=("easy", "medium"),
            eval_goal="navigation",
            tags=("python",),
            task_ids=("task-1", "task-2"),
        )
        assert suite.task_types == ("micro_probe",)
        assert suite.difficulties == ("easy", "medium")
        assert suite.eval_goal == "navigation"
        assert suite.tags == ("python",)
        assert suite.task_ids == ("task-1", "task-2")


# ---------------------------------------------------------------------------
# TOML loader
# ---------------------------------------------------------------------------


class TestLoadSuite:
    def test_minimal(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "suite.toml"
        toml_file.write_text(textwrap.dedent("""\
            [suite]
            name = "minimal"
            """))
        suite = load_suite(toml_file)
        assert suite.name == "minimal"
        assert suite.task_types == ()
        assert suite.difficulties == ()

    def test_full_manifest(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "suite.toml"
        toml_file.write_text(textwrap.dedent("""\
            [suite]
            name = "navigation-benchmark"
            description = "Tasks focused on code navigation"
            task_dir = "nav_tasks"
            task_types = ["micro_probe", "architecture_comprehension"]
            difficulties = ["easy", "medium"]
            eval_goal = "navigation"
            tags = ["python", "go"]
            task_ids = ["task-abc", "task-def"]
            """))
        suite = load_suite(toml_file)
        assert suite.name == "navigation-benchmark"
        assert suite.description == "Tasks focused on code navigation"
        assert suite.task_dir == "nav_tasks"
        assert suite.task_types == ("micro_probe", "architecture_comprehension")
        assert suite.difficulties == ("easy", "medium")
        assert suite.eval_goal == "navigation"
        assert suite.tags == ("python", "go")
        assert suite.task_ids == ("task-abc", "task-def")

    def test_missing_suite_section(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "suite.toml"
        toml_file.write_text("[other]\nfoo = 1\n")
        with pytest.raises(ValueError, match="Missing required \\[suite\\] section"):
            load_suite(toml_file)

    def test_missing_name(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "suite.toml"
        toml_file.write_text("[suite]\ndescription = 'no name'\n")
        with pytest.raises(ValueError, match="Missing required field 'name'"):
            load_suite(toml_file)

    def test_invalid_task_type(self, tmp_path: Path) -> None:
        toml_file = tmp_path / "suite.toml"
        toml_file.write_text(textwrap.dedent("""\
            [suite]
            name = "bad"
            task_types = ["nonexistent_type"]
            """))
        with pytest.raises(ValueError, match="Unknown task_type"):
            load_suite(toml_file)

    def test_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_suite(tmp_path / "missing.toml")


# ---------------------------------------------------------------------------
# Task filtering
# ---------------------------------------------------------------------------


def _make_task_dir(
    parent: Path,
    name: str,
    *,
    task_type: str = "sdlc_code_change",
    difficulty: str = "medium",
    tags: list[str] | None = None,
) -> Path:
    """Create a minimal task directory with instruction.md and task.toml."""
    td = parent / name
    td.mkdir()
    (td / "instruction.md").write_text(f"# {name}\nDo the thing.\n")

    tags_toml = ""
    if tags:
        tags_list = ", ".join(f'"{t}"' for t in tags)
        tags_toml = f"tags = [{tags_list}]"

    (td / "task.toml").write_text(textwrap.dedent(f"""\
        [task]
        id = "{name}"
        repo = "test/repo"
        difficulty = "{difficulty}"
        task_type = "{task_type}"
        {tags_toml}

        [metadata]
        name = "{name}"

        [verification]
        type = "test_script"
        command = "bash tests/test.sh"
        """))
    return td


class TestFilterTasksBySuite:
    def test_no_filters_returns_all(self, tmp_path: Path) -> None:
        from codeprobe.cli.run_cmd import _filter_tasks_by_suite

        td1 = _make_task_dir(tmp_path, "task-1")
        td2 = _make_task_dir(tmp_path, "task-2")
        suite = Suite(name="all")
        result = _filter_tasks_by_suite([td1, td2], suite)
        assert result == [td1, td2]

    def test_filter_by_task_type(self, tmp_path: Path) -> None:
        from codeprobe.cli.run_cmd import _filter_tasks_by_suite

        td_probe = _make_task_dir(tmp_path, "probe-1", task_type="micro_probe")
        _make_task_dir(tmp_path, "sdlc-1", task_type="sdlc_code_change")
        suite = Suite(name="probes", task_types=("micro_probe",))
        result = _filter_tasks_by_suite([td_probe, tmp_path / "sdlc-1"], suite)
        assert result == [td_probe]

    def test_filter_by_difficulty(self, tmp_path: Path) -> None:
        from codeprobe.cli.run_cmd import _filter_tasks_by_suite

        td_easy = _make_task_dir(tmp_path, "easy-1", difficulty="easy")
        _make_task_dir(tmp_path, "hard-1", difficulty="hard")
        suite = Suite(name="easy-only", difficulties=("easy",))
        result = _filter_tasks_by_suite([td_easy, tmp_path / "hard-1"], suite)
        assert result == [td_easy]

    def test_filter_by_tags(self, tmp_path: Path) -> None:
        from codeprobe.cli.run_cmd import _filter_tasks_by_suite

        td_py = _make_task_dir(tmp_path, "py-task", tags=["python"])
        _make_task_dir(tmp_path, "go-task", tags=["go"])
        suite = Suite(name="python-only", tags=("python",))
        result = _filter_tasks_by_suite([td_py, tmp_path / "go-task"], suite)
        assert result == [td_py]

    def test_filter_by_task_ids(self, tmp_path: Path) -> None:
        from codeprobe.cli.run_cmd import _filter_tasks_by_suite

        td1 = _make_task_dir(tmp_path, "task-1")
        td2 = _make_task_dir(tmp_path, "task-2")
        td3 = _make_task_dir(tmp_path, "task-3")
        suite = Suite(name="subset", task_ids=("task-1", "task-3"))
        result = _filter_tasks_by_suite([td1, td2, td3], suite)
        assert result == [td1, td3]

    def test_combined_filters(self, tmp_path: Path) -> None:
        from codeprobe.cli.run_cmd import _filter_tasks_by_suite

        td_match = _make_task_dir(
            tmp_path, "match", task_type="micro_probe", difficulty="easy"
        )
        _make_task_dir(
            tmp_path, "wrong-type", task_type="sdlc_code_change", difficulty="easy"
        )
        _make_task_dir(
            tmp_path, "wrong-diff", task_type="micro_probe", difficulty="hard"
        )
        suite = Suite(
            name="combined",
            task_types=("micro_probe",),
            difficulties=("easy",),
        )
        result = _filter_tasks_by_suite(
            [td_match, tmp_path / "wrong-type", tmp_path / "wrong-diff"],
            suite,
        )
        assert result == [td_match]

    def test_skips_tasks_without_metadata(self, tmp_path: Path) -> None:
        from codeprobe.cli.run_cmd import _filter_tasks_by_suite

        # Task with metadata
        td_good = _make_task_dir(tmp_path, "good", task_type="micro_probe")
        # Task without metadata file
        td_bare = tmp_path / "bare"
        td_bare.mkdir()
        (td_bare / "instruction.md").write_text("# bare\n")

        suite = Suite(name="typed", task_types=("micro_probe",))
        result = _filter_tasks_by_suite([td_good, td_bare], suite)
        assert result == [td_good]
