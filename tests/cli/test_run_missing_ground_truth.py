"""Regression test for the missing-ground-truth preflight check (codeprobe-yxex).

A stale or interrupted `codeprobe mine` run can leave an artifact_eval/dual
task without tests/ground_truth.json. Before this check, `codeprobe run`
would score every trial on such a task ``verifier_error`` — indistinguishable
from an agent failure — instead of rejecting it loudly up front.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from codeprobe.cli.errors import DiagnosticError
from codeprobe.cli.run_cmd import _check_ground_truth_present


def _make_task_dir(
    parent: Path,
    name: str,
    *,
    verification_mode: str = "test_script",
) -> Path:
    """Create a minimal task directory with instruction.md and task.toml."""
    td = parent / name
    td.mkdir()
    (td / "instruction.md").write_text(f"# {name}\nDo the thing.\n")
    (td / "task.toml").write_text(
        textwrap.dedent(f"""\
            [task]
            id = "{name}"
            repo = "test/repo"

            [metadata]
            name = "{name}"

            [verification]
            type = "test_script"
            command = "bash tests/test.sh"
            verification_mode = "{verification_mode}"
            """)
    )
    return td


class TestCheckGroundTruthPresent:
    def test_artifact_eval_missing_ground_truth_raises(self, tmp_path: Path) -> None:
        td = _make_task_dir(tmp_path, "task-1", verification_mode="artifact_eval")

        with pytest.raises(DiagnosticError) as exc_info:
            _check_ground_truth_present([td], str(tmp_path))

        err = exc_info.value
        assert err.code == "MISSING_GROUND_TRUTH"
        assert "task-1" in err.message

    def test_dual_missing_ground_truth_raises(self, tmp_path: Path) -> None:
        td = _make_task_dir(tmp_path, "task-2", verification_mode="dual")

        with pytest.raises(DiagnosticError) as exc_info:
            _check_ground_truth_present([td], str(tmp_path))

        assert exc_info.value.code == "MISSING_GROUND_TRUTH"
        assert "task-2" in exc_info.value.detail["missing_ground_truth_tasks"]

    def test_artifact_eval_with_ground_truth_does_not_raise(self, tmp_path: Path) -> None:
        td = _make_task_dir(tmp_path, "task-3", verification_mode="artifact_eval")
        (td / "tests").mkdir()
        (td / "tests" / "ground_truth.json").write_text(
            json.dumps({"answer_type": "count", "answer": 1})
        )

        _check_ground_truth_present([td], str(tmp_path))

    def test_test_script_missing_ground_truth_does_not_raise(self, tmp_path: Path) -> None:
        """The vast majority of SDLC tasks have no ground_truth.json at all."""
        td = _make_task_dir(tmp_path, "task-4", verification_mode="test_script")

        _check_ground_truth_present([td], str(tmp_path))

    def test_legacy_root_ground_truth_counts_as_present(self, tmp_path: Path) -> None:
        td = _make_task_dir(tmp_path, "task-5", verification_mode="dual")
        (td / "ground_truth.json").write_text(
            json.dumps({"answer_type": "count", "answer": 1})
        )

        _check_ground_truth_present([td], str(tmp_path))

    def test_skips_tasks_without_metadata(self, tmp_path: Path) -> None:
        td_bare = tmp_path / "bare"
        td_bare.mkdir()
        (td_bare / "instruction.md").write_text("# bare\n")

        _check_ground_truth_present([td_bare], str(tmp_path))


class TestPreflightRejectsUnusableOracles:
    """Preflight validates the oracle's CONTENT, not just its existence.

    Checking only ``exists()`` let a malformed oracle through preflight and
    into a full run, where every trial scored verifier_error. The empty
    ``{}`` fixture this suite used to treat as valid was itself masking the
    scorer defect (codeprobe-sh8c).
    """

    def _dual_task(self, tmp_path: Path, name: str) -> Path:
        return _make_task_dir(tmp_path, name, verification_mode="dual")

    def _write_gt(self, td: Path, content: str) -> None:
        (td / "tests").mkdir(exist_ok=True)
        (td / "tests" / "ground_truth.json").write_text(content)

    def _expect_invalid(self, td: Path, tmp_path: Path, fragment: str) -> None:
        with pytest.raises(DiagnosticError) as exc_info:
            _check_ground_truth_present([td], str(tmp_path))
        assert exc_info.value.code == "INVALID_GROUND_TRUTH"
        assert fragment in str(exc_info.value.detail["invalid_ground_truth_tasks"])

    def test_rejects_empty_object(self, tmp_path: Path) -> None:
        td = self._dual_task(tmp_path, "t-empty")
        self._write_gt(td, "{}")
        self._expect_invalid(td, tmp_path, "t-empty")

    def test_rejects_invalid_json(self, tmp_path: Path) -> None:
        td = self._dual_task(tmp_path, "t-badjson")
        self._write_gt(td, "{not json")
        self._expect_invalid(td, tmp_path, "invalid JSON")

    def test_rejects_non_object(self, tmp_path: Path) -> None:
        td = self._dual_task(tmp_path, "t-list")
        self._write_gt(td, json.dumps(["a.py"]))
        self._expect_invalid(td, tmp_path, "must be a JSON object")

    def test_rejects_directory_valued_ground_truth(self, tmp_path: Path) -> None:
        td = self._dual_task(tmp_path, "t-dir")
        (td / "tests").mkdir(exist_ok=True)
        (td / "tests" / "ground_truth.json").mkdir()
        self._expect_invalid(td, tmp_path, "not a regular file")

    def test_rejects_symlinked_tests_directory(self, tmp_path: Path) -> None:
        outside = tmp_path / "outside-tests"
        outside.mkdir()
        (outside / "ground_truth.json").write_text(
            json.dumps({"answer_type": "count", "answer": 1})
        )
        td = self._dual_task(tmp_path, "t-tests-link")
        (td / "tests").symlink_to(outside, target_is_directory=True)

        self._expect_invalid(td, tmp_path, "symlink")

    def test_rejects_symlinked_task_directory(self, tmp_path: Path) -> None:
        outside_parent = tmp_path / "outside-parent"
        outside_parent.mkdir()
        outside = _make_task_dir(
            outside_parent,
            "external-task",
            verification_mode="dual",
        )
        self._write_gt(
            outside,
            json.dumps({"answer_type": "count", "answer": 1}),
        )
        task_link = tmp_path / "t-task-link"
        task_link.symlink_to(outside, target_is_directory=True)

        self._expect_invalid(task_link, tmp_path, "symlink")

    def test_rejects_oversized_ground_truth(self, tmp_path: Path) -> None:
        from codeprobe.core.scoring.scorers import _MAX_GROUND_TRUTH_BYTES

        td = self._dual_task(tmp_path, "t-big")
        self._write_gt(td, " " * (_MAX_GROUND_TRUTH_BYTES + 1))
        self._expect_invalid(td, tmp_path, "oversized")

    def test_rejects_unknown_answer_type(self, tmp_path: Path) -> None:
        td = self._dual_task(tmp_path, "t-unknown")
        self._write_gt(td, json.dumps({"answer_type": "integer", "answer": 5}))
        self._expect_invalid(td, tmp_path, "t-unknown")

    def test_rejects_registered_but_unloadable_answer_type(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from codeprobe.core import registry

        answer_type = "broken_plugin_type"

        def _unloadable(name: str) -> object:
            assert name == answer_type
            raise KeyError(f"oracle scorer {name!r} could not be loaded")

        monkeypatch.setattr(registry, "resolve_oracle_scorer", _unloadable)
        td = self._dual_task(tmp_path, "t-unloadable")
        self._write_gt(
            td,
            json.dumps({"answer_type": answer_type, "answer": "expected"}),
        )

        self._expect_invalid(td, tmp_path, "could not be loaded")

    def test_rejects_deeply_nested_json(self, tmp_path: Path) -> None:
        td = self._dual_task(tmp_path, "t-deep")
        self._write_gt(td, "[" * 4000 + "0" + "]" * 4000)

        self._expect_invalid(td, tmp_path, "invalid JSON")

    @pytest.mark.parametrize(
        ("payload", "fragment"),
        [
            (
                {
                    "checks": [
                        {
                            "answer_type": "file_list",
                            "answer": "a.py",
                            "weight": 1.0,
                        }
                    ]
                },
                "requires a non-empty list",
            ),
            (
                {
                    "answer_type": "count",
                    "answer": 1,
                    "confidence": "high",
                },
                "confidence",
            ),
            (
                {
                    "checks": [
                        {
                            "answer_type": "count",
                            "answer": 1,
                            "weight": 10**1000,
                        }
                    ]
                },
                "weight",
            ),
            (
                {"answer_type": "count", "answer": 1.9},
                "count",
            ),
            (
                {"answer_type": "file_list", "answer": ["  "]},
                "non-empty string",
            ),
            (
                {"expected": [1]},
                "expected",
            ),
        ],
        ids=[
            "builtin-answer-shape",
            "confidence",
            "weight-overflow",
            "fractional-count",
            "whitespace-list-item",
            "legacy-non-string",
        ],
    )
    def test_rejects_schema_invalid_oracle_before_run(
        self,
        tmp_path: Path,
        payload: dict[str, object],
        fragment: str,
    ) -> None:
        td = self._dual_task(tmp_path, "t-schema-invalid")
        self._write_gt(td, json.dumps(payload))

        self._expect_invalid(td, tmp_path, fragment)

    def test_rejects_missing_checks_and_expected(self, tmp_path: Path) -> None:
        td = self._dual_task(tmp_path, "t-noschema")
        self._write_gt(td, json.dumps({"confidence": 0.9}))
        self._expect_invalid(td, tmp_path, "t-noschema")

    def test_unparseable_metadata_does_not_abort_the_run(self, tmp_path: Path) -> None:
        """Metadata validity is not this check's job.

        load_task() raises for an unknown reward_type, and the valid set is
        environment-dependent (it includes entry-point scorers), so a task
        referencing an uninstalled plugin must not abort an otherwise fine
        run at oracle preflight.
        """
        td = tmp_path / "t-badmeta"
        td.mkdir()
        (td / "instruction.md").write_text("# x\n")
        (td / "task.toml").write_text("this is not valid toml {{{\n")

        _check_ground_truth_present([td], str(tmp_path))

    def test_valid_oracle_passes(self, tmp_path: Path) -> None:
        td = self._dual_task(tmp_path, "t-ok")
        self._write_gt(td, json.dumps({"answer_type": "count", "answer": 3}))

        _check_ground_truth_present([td], str(tmp_path))
