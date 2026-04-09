"""Tests for Task data model extensions: task_type and verification_mode fields."""

from __future__ import annotations

from dataclasses import replace

import pytest

from codeprobe.models.task import (
    TASK_TYPES,
    VERIFICATION_MODES,
    Task,
    TaskMetadata,
    TaskVerification,
)


class TestTaskTypes:
    def test_contains_all_expected_types(self) -> None:
        expected = {
            "sdlc_code_change",
            "micro_probe",
            "mcp_tool_usage",
            "architecture_comprehension",
            "org_scale_cross_repo",
        }
        assert TASK_TYPES == expected

    def test_is_frozenset(self) -> None:
        assert isinstance(TASK_TYPES, frozenset)


class TestVerificationModes:
    def test_contains_all_expected_modes(self) -> None:
        expected = {"test_script", "artifact_eval", "dual"}
        assert VERIFICATION_MODES == expected

    def test_is_frozenset(self) -> None:
        assert isinstance(VERIFICATION_MODES, frozenset)


class TestTaskMetadata:
    def test_task_type_default(self) -> None:
        m = TaskMetadata(name="test")
        assert m.task_type == "sdlc_code_change"

    def test_task_type_custom(self) -> None:
        m = TaskMetadata(name="test", task_type="micro_probe")
        assert m.task_type == "micro_probe"

    def test_task_type_in_task_types(self) -> None:
        m = TaskMetadata(name="test")
        assert m.task_type in TASK_TYPES

    def test_frozen(self) -> None:
        m = TaskMetadata(name="test")
        with pytest.raises(AttributeError):
            m.task_type = "micro_probe"  # type: ignore[misc]

    def test_replace_task_type(self) -> None:
        m = TaskMetadata(name="test")
        m2 = replace(m, task_type="mcp_tool_usage")
        assert m2.task_type == "mcp_tool_usage"
        assert m.task_type == "sdlc_code_change"


class TestTaskVerification:
    def test_verification_mode_default(self) -> None:
        v = TaskVerification()
        assert v.verification_mode == "test_script"

    def test_verification_mode_custom(self) -> None:
        v = TaskVerification(verification_mode="artifact_eval")
        assert v.verification_mode == "artifact_eval"

    def test_verification_mode_in_modes(self) -> None:
        v = TaskVerification()
        assert v.verification_mode in VERIFICATION_MODES

    def test_eval_command_default(self) -> None:
        v = TaskVerification()
        assert v.eval_command == ""

    def test_ground_truth_path_default(self) -> None:
        v = TaskVerification()
        assert v.ground_truth_path == "tests/ground_truth.json"

    def test_answer_schema_default(self) -> None:
        v = TaskVerification()
        assert v.answer_schema == ""

    def test_custom_artifact_fields(self) -> None:
        v = TaskVerification(
            verification_mode="dual",
            eval_command="python eval.py",
            ground_truth_path="data/truth.json",
            answer_schema='{"type": "object"}',
        )
        assert v.verification_mode == "dual"
        assert v.eval_command == "python eval.py"
        assert v.ground_truth_path == "data/truth.json"
        assert v.answer_schema == '{"type": "object"}'

    def test_frozen(self) -> None:
        v = TaskVerification()
        with pytest.raises(AttributeError):
            v.verification_mode = "dual"  # type: ignore[misc]

    def test_replace_verification_fields(self) -> None:
        v = TaskVerification()
        v2 = replace(
            v, verification_mode="artifact_eval", eval_command="python check.py"
        )
        assert v2.verification_mode == "artifact_eval"
        assert v2.eval_command == "python check.py"
        assert v.verification_mode == "test_script"
        assert v.eval_command == ""


class TestTaskRoundTrip:
    """Ensure new fields survive dataclass asdict serialization."""

    def test_asdict_includes_new_fields(self) -> None:
        from dataclasses import asdict

        task = Task(
            id="test-001",
            repo="example/repo",
            metadata=TaskMetadata(name="test", task_type="micro_probe"),
            verification=TaskVerification(verification_mode="dual"),
        )
        d = asdict(task)
        assert d["metadata"]["task_type"] == "micro_probe"
        assert d["verification"]["verification_mode"] == "dual"
        assert d["verification"]["eval_command"] == ""
        assert d["verification"]["ground_truth_path"] == "tests/ground_truth.json"
        assert d["verification"]["answer_schema"] == ""
