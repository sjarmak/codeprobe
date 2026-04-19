"""Tests for the task-type registry and the `--task-type` / `--list-task-types` CLI."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import click.testing
import pytest

from codeprobe.cli import main as cli_main
from codeprobe.mining.task_types import (
    TASK_TYPE_REGISTRY,
    TaskTypeInfo,
    get_task_type,
    list_task_types,
    task_type_names,
)
from codeprobe.models.task import TASK_TYPES as MODEL_TASK_TYPES

_CSB_SUITES_PATH = Path(
    "/home/ds/projects/CodeScaleBench/benchmarks/suites/csb-v2-dual264.json"
)


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


class TestTaskTypeRegistry:
    def test_at_least_three_types_registered(self) -> None:
        assert len(TASK_TYPE_REGISTRY) >= 3

    def test_names_are_sorted_and_unique(self) -> None:
        names = task_type_names()
        assert names == sorted(names)
        assert len(names) == len(set(names))

    def test_descriptions_are_long_enough(self) -> None:
        for name, info in TASK_TYPE_REGISTRY.items():
            assert len(info.description) >= 40, (
                f"Task type {name!r} description too short: "
                f"{len(info.description)} chars"
            )

    def test_primary_suite_is_in_suites_tuple(self) -> None:
        for name, info in TASK_TYPE_REGISTRY.items():
            assert info.csb_suite in info.csb_suites, name

    def test_dispatch_keys_are_known(self) -> None:
        valid_keys = {"sdlc", "probe", "comprehension", "org_scale", "mixed"}
        for name, info in TASK_TYPE_REGISTRY.items():
            assert info.dispatch_key in valid_keys, (name, info.dispatch_key)

    def test_all_registered_types_except_mixed_are_in_model_task_types(self) -> None:
        for name in TASK_TYPE_REGISTRY:
            if name == "mixed":
                continue  # meta-type used at CLI layer, not persisted on Task
            assert name in MODEL_TASK_TYPES, (
                f"{name} not in codeprobe.models.task.TASK_TYPES"
            )

    def test_get_task_type_raises_keyerror_on_unknown(self) -> None:
        with pytest.raises(KeyError):
            get_task_type("this-type-does-not-exist")

    def test_list_task_types_yields_all_entries(self) -> None:
        entries = list(list_task_types())
        assert len(entries) == len(TASK_TYPE_REGISTRY)
        assert all(isinstance(e[1], TaskTypeInfo) for e in entries)


# ---------------------------------------------------------------------------
# CSB suite grounding
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _CSB_SUITES_PATH.exists(),
    reason="CodeScaleBench suite file not available in this environment",
)
def test_all_registered_suites_exist_in_csb_dual264() -> None:
    suites = set(json.loads(_CSB_SUITES_PATH.read_text())["suites"].keys())
    for name, info in TASK_TYPE_REGISTRY.items():
        for suite_id in info.csb_suites:
            assert suite_id in suites, (
                f"Task type {name!r} maps to suite {suite_id!r} which is not "
                f"in csb-v2-dual264.json"
            )


# ---------------------------------------------------------------------------
# CLI behavior — --list-task-types and --task-type validation
# ---------------------------------------------------------------------------


class TestMineCLIWithTaskType:
    def test_list_task_types_prints_registered_set(self) -> None:
        runner = click.testing.CliRunner()
        result = runner.invoke(cli_main, ["mine", "--list-task-types"])
        assert result.exit_code == 0, result.output
        for name, info in TASK_TYPE_REGISTRY.items():
            assert name in result.output
            assert info.csb_suite in result.output

    def test_invalid_task_type_is_rejected_with_valid_set(self) -> None:
        runner = click.testing.CliRunner()
        result = runner.invoke(cli_main, ["mine", "--task-type", "bogus", "."])
        assert result.exit_code != 0
        # Click echoes the choices it accepts
        for name in task_type_names():
            assert name in result.output

    def test_valid_task_type_passes_validation(self, tmp_path: Path) -> None:
        """An accepted --task-type reaches run_mine (we stub the executor)."""
        runner = click.testing.CliRunner()
        with patch("codeprobe.cli.mine_cmd.run_mine") as mock_run:
            # Point PATH at tmp_path so validation of "path" argument passes.
            result = runner.invoke(
                cli_main,
                [
                    "mine",
                    "--task-type",
                    "micro_probe",
                    "--no-interactive",
                    str(tmp_path),
                ],
            )
        assert result.exit_code == 0, result.output
        mock_run.assert_called_once()
        # The override must be passed through as task_type_override.
        kwargs = mock_run.call_args.kwargs
        assert kwargs["task_type_override"] == "micro_probe"


# ---------------------------------------------------------------------------
# Suitability pre-check
# ---------------------------------------------------------------------------


class TestSuitabilityCheck:
    def test_warns_for_org_scale_on_tiny_repo(self, tmp_path: Path) -> None:
        # Tiny repo: just a single Python file.
        (tmp_path / "main.py").write_text("print('hi')\n")
        from codeprobe.cli.mine_cmd import _suitability_warnings

        warnings = _suitability_warnings("org_scale_cross_repo", tmp_path)
        assert any("org_scale" in w for w in warnings), warnings

    def test_warns_for_sdlc_without_tests_dir(self, tmp_path: Path) -> None:
        (tmp_path / "src").mkdir()
        for i in range(3):
            (tmp_path / "src" / f"mod_{i}.py").write_text("x = 1\n")
        from codeprobe.cli.mine_cmd import _suitability_warnings

        warnings = _suitability_warnings("sdlc_code_change", tmp_path)
        assert any("tests/" in w for w in warnings), warnings

    def test_no_warning_for_micro_probe_on_tiny_repo(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("def f(): pass\n")
        from codeprobe.cli.mine_cmd import _suitability_warnings

        assert _suitability_warnings("micro_probe", tmp_path) == []

    def test_interactive_decline_aborts(self, tmp_path: Path) -> None:
        """When the user declines the suitability prompt, we stop."""
        (tmp_path / "main.py").write_text("print('hi')\n")
        from codeprobe.cli.mine_cmd import _run_suitability_check

        with patch("click.confirm", return_value=False) as mock_confirm:
            proceed = _run_suitability_check(
                "org_scale_cross_repo", tmp_path, interactive=True
            )
        assert proceed is False
        mock_confirm.assert_called_once()

    def test_non_interactive_does_not_block(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("print('hi')\n")
        from codeprobe.cli.mine_cmd import _run_suitability_check

        # Even with warnings, non-interactive callers must not block.
        proceed = _run_suitability_check(
            "org_scale_cross_repo", tmp_path, interactive=False
        )
        assert proceed is True
