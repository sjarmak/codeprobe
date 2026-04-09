"""Tests for ProbeTaskAdapter — converts Probe objects to Task directory layout."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.probe.adapter import ProbeTaskAdapter
from codeprobe.probe.generator import Probe

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_probe() -> Probe:
    return Probe(
        template_name="find_function",
        category="probe_navigate",
        prompt="What file contains the function `foo`? Reply with just the relative file path.",
        answer="src/utils.py",
        answer_type="file_path",
        difficulty="easy",
        capability_tags=("navigation", "symbol_search"),
        time_limit_sec=30,
    )


@pytest.fixture()
def sample_probes() -> list[Probe]:
    return [
        Probe(
            template_name="find_function",
            category="probe_navigate",
            prompt="Where is `foo`?",
            answer="src/foo.py",
            answer_type="file_path",
            difficulty="easy",
            capability_tags=("navigation",),
        ),
        Probe(
            template_name="count_callers",
            category="probe_comprehend",
            prompt="How many callers for `bar`?",
            answer="3",
            answer_type="integer",
            difficulty="medium",
            capability_tags=("comprehension", "cross_reference"),
        ),
        Probe(
            template_name="return_type",
            category="probe_comprehend",
            prompt="What does `Baz.run` return?",
            answer="bool",
            answer_type="text",
            difficulty="medium",
            capability_tags=("comprehension", "type_analysis"),
        ),
    ]


# ---------------------------------------------------------------------------
# to_task_directory
# ---------------------------------------------------------------------------


class TestToTaskDirectory:
    def test_creates_directory_structure(
        self,
        tmp_path: Path,
        sample_probe: Probe,
    ) -> None:
        task_dir = ProbeTaskAdapter.to_task_directory(
            sample_probe,
            tmp_path,
            index=0,
        )
        assert task_dir.is_dir()
        assert (task_dir / "instruction.md").is_file()
        assert (task_dir / "task.toml").is_file()
        assert (task_dir / "tests" / "test.sh").is_file()
        assert (task_dir / "tests" / "ground_truth.json").is_file()

    def test_task_id_uses_template_and_index(
        self,
        tmp_path: Path,
        sample_probe: Probe,
    ) -> None:
        task_dir = ProbeTaskAdapter.to_task_directory(
            sample_probe,
            tmp_path,
            index=7,
        )
        assert task_dir.name == "probe-findfunction-007"

    def test_instruction_md_contains_prompt(
        self,
        tmp_path: Path,
        sample_probe: Probe,
    ) -> None:
        task_dir = ProbeTaskAdapter.to_task_directory(
            sample_probe,
            tmp_path,
            index=0,
        )
        content = (task_dir / "instruction.md").read_text(encoding="utf-8")
        assert sample_probe.prompt in content

    def test_task_toml_has_task_type(
        self,
        tmp_path: Path,
        sample_probe: Probe,
    ) -> None:
        task_dir = ProbeTaskAdapter.to_task_directory(
            sample_probe,
            tmp_path,
            index=0,
        )
        toml_text = (task_dir / "task.toml").read_text(encoding="utf-8")
        assert 'task_type = "micro_probe"' in toml_text

    def test_task_toml_has_verification_mode(
        self,
        tmp_path: Path,
        sample_probe: Probe,
    ) -> None:
        task_dir = ProbeTaskAdapter.to_task_directory(
            sample_probe,
            tmp_path,
            index=0,
        )
        toml_text = (task_dir / "task.toml").read_text(encoding="utf-8")
        assert 'mode = "artifact_eval"' in toml_text

    def test_task_toml_has_repo_name(
        self,
        tmp_path: Path,
        sample_probe: Probe,
    ) -> None:
        task_dir = ProbeTaskAdapter.to_task_directory(
            sample_probe,
            tmp_path,
            index=0,
            repo_name="my-repo",
        )
        toml_text = (task_dir / "task.toml").read_text(encoding="utf-8")
        assert 'repo = "my-repo"' in toml_text

    def test_task_toml_no_repo_when_none(
        self,
        tmp_path: Path,
        sample_probe: Probe,
    ) -> None:
        task_dir = ProbeTaskAdapter.to_task_directory(
            sample_probe,
            tmp_path,
            index=0,
        )
        toml_text = (task_dir / "task.toml").read_text(encoding="utf-8")
        assert "repo =" not in toml_text

    def test_task_toml_has_capability_tags(
        self,
        tmp_path: Path,
        sample_probe: Probe,
    ) -> None:
        task_dir = ProbeTaskAdapter.to_task_directory(
            sample_probe,
            tmp_path,
            index=0,
        )
        toml_text = (task_dir / "task.toml").read_text(encoding="utf-8")
        assert '"navigation"' in toml_text
        assert '"symbol_search"' in toml_text

    def test_ground_truth_json_format(
        self,
        tmp_path: Path,
        sample_probe: Probe,
    ) -> None:
        task_dir = ProbeTaskAdapter.to_task_directory(
            sample_probe,
            tmp_path,
            index=0,
        )
        gt = json.loads(
            (task_dir / "tests" / "ground_truth.json").read_text(encoding="utf-8"),
        )
        assert gt["answer"] == "src/utils.py"
        assert gt["answer_type"] == "file_path"
        assert gt["confidence"] == 1.0
        assert gt["provenance"] == "deterministic"

    def test_ground_truth_has_exactly_four_keys(
        self,
        tmp_path: Path,
        sample_probe: Probe,
    ) -> None:
        task_dir = ProbeTaskAdapter.to_task_directory(
            sample_probe,
            tmp_path,
            index=0,
        )
        gt = json.loads(
            (task_dir / "tests" / "ground_truth.json").read_text(encoding="utf-8"),
        )
        assert set(gt.keys()) == {"answer", "answer_type", "confidence", "provenance"}

    def test_test_sh_is_executable(
        self,
        tmp_path: Path,
        sample_probe: Probe,
    ) -> None:
        task_dir = ProbeTaskAdapter.to_task_directory(
            sample_probe,
            tmp_path,
            index=0,
        )
        import os
        import stat

        mode = os.stat(task_dir / "tests" / "test.sh").st_mode
        assert mode & stat.S_IXUSR


# ---------------------------------------------------------------------------
# convert_batch
# ---------------------------------------------------------------------------


class TestConvertBatch:
    def test_creates_all_task_dirs(
        self,
        tmp_path: Path,
        sample_probes: list[Probe],
    ) -> None:
        created = ProbeTaskAdapter.convert_batch(sample_probes, tmp_path)
        assert len(created) == 3
        for task_dir in created:
            assert task_dir.is_dir()
            assert (task_dir / "instruction.md").is_file()
            assert (task_dir / "task.toml").is_file()

    def test_unique_task_ids(
        self,
        tmp_path: Path,
        sample_probes: list[Probe],
    ) -> None:
        created = ProbeTaskAdapter.convert_batch(sample_probes, tmp_path)
        names = [d.name for d in created]
        assert len(names) == len(set(names))

    def test_indexes_are_sequential(
        self,
        tmp_path: Path,
        sample_probes: list[Probe],
    ) -> None:
        created = ProbeTaskAdapter.convert_batch(sample_probes, tmp_path)
        # Extract index suffixes
        indexes = [d.name.split("-")[-1] for d in created]
        assert indexes == ["000", "001", "002"]

    def test_repo_name_propagated(
        self,
        tmp_path: Path,
        sample_probes: list[Probe],
    ) -> None:
        created = ProbeTaskAdapter.convert_batch(
            sample_probes,
            tmp_path,
            repo_name="test-repo",
        )
        for task_dir in created:
            toml_text = (task_dir / "task.toml").read_text(encoding="utf-8")
            assert 'repo = "test-repo"' in toml_text

    def test_empty_list(self, tmp_path: Path) -> None:
        created = ProbeTaskAdapter.convert_batch([], tmp_path)
        assert created == []


# ---------------------------------------------------------------------------
# Probe dataclass is unchanged (frozen, no new fields)
# ---------------------------------------------------------------------------


class TestProbeUnchanged:
    def test_probe_is_frozen(self) -> None:
        p = Probe(
            template_name="t",
            category="c",
            prompt="p",
            answer="a",
            answer_type="text",
            difficulty="easy",
        )
        with pytest.raises(AttributeError):
            p.template_name = "other"  # type: ignore[misc]

    def test_probe_has_expected_fields(self) -> None:
        expected = {
            "template_name",
            "category",
            "prompt",
            "answer",
            "answer_type",
            "difficulty",
            "capability_tags",
            "time_limit_sec",
        }
        from dataclasses import fields

        actual = {f.name for f in fields(Probe)}
        assert actual == expected
