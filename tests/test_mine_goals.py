"""Tests for eval goal mappings, --goal flag, and cold-start fallback."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from codeprobe.cli.mine_cmd import (
    _EVAL_GOALS,
    _GOAL_FLAG_MAP,
    _resolve_task_type,
)

# ---------------------------------------------------------------------------
# _EVAL_GOALS structure
# ---------------------------------------------------------------------------


class TestEvalGoals:
    """Verify _EVAL_GOALS includes task_type as the 4th element."""

    def test_all_entries_have_four_elements(self) -> None:
        for key, entry in _EVAL_GOALS.items():
            assert (
                len(entry) == 4
            ), f"Goal {key} should have 4 elements, got {len(entry)}"

    def test_quality_goal(self) -> None:
        name, min_files, bias, task_type = _EVAL_GOALS["1"]
        assert name == "Code quality comparison"
        assert min_files == 2
        assert bias == "mixed"
        assert task_type == "sdlc_code_change"

    def test_navigation_goal(self) -> None:
        name, min_files, bias, task_type = _EVAL_GOALS["2"]
        assert name == "Codebase navigation"
        assert min_files == 0
        assert bias == "mixed"
        assert task_type == "architecture_comprehension"

    def test_mcp_goal(self) -> None:
        name, min_files, bias, task_type = _EVAL_GOALS["3"]
        assert name == "MCP / tool benefit"
        assert min_files == 6
        assert bias == "hard"
        assert task_type == "mcp_tool_usage"

    def test_general_goal(self) -> None:
        name, min_files, bias, task_type = _EVAL_GOALS["4"]
        assert name == "General benchmarking"
        assert min_files == 0
        assert bias == "balanced"
        assert task_type == "mixed"


# ---------------------------------------------------------------------------
# _GOAL_FLAG_MAP
# ---------------------------------------------------------------------------


class TestGoalFlagMap:
    """Verify --goal flag values map to correct _EVAL_GOALS keys."""

    def test_quality_maps_to_key_1(self) -> None:
        assert _GOAL_FLAG_MAP["quality"] == "1"

    def test_navigation_maps_to_key_2(self) -> None:
        assert _GOAL_FLAG_MAP["navigation"] == "2"

    def test_mcp_maps_to_key_3(self) -> None:
        assert _GOAL_FLAG_MAP["mcp"] == "3"

    def test_general_maps_to_key_4(self) -> None:
        assert _GOAL_FLAG_MAP["general"] == "4"

    def test_all_flag_values_resolve_to_valid_goals(self) -> None:
        for flag_val, goal_key in _GOAL_FLAG_MAP.items():
            assert (
                goal_key in _EVAL_GOALS
            ), f"Flag '{flag_val}' maps to key '{goal_key}' not in _EVAL_GOALS"


# ---------------------------------------------------------------------------
# _resolve_task_type — cold-start detection
# ---------------------------------------------------------------------------


class TestResolveTaskTypeColdStart:
    """Cold-start: 0 merge commits triggers micro_probe fallback."""

    @patch(
        "codeprobe.cli.mine_cmd._cold_start_check",
        return_value=True,
    )
    def test_navigation_falls_back_on_cold_start(self, _csc, tmp_path, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            result = _resolve_task_type("architecture_comprehension", tmp_path, "auto")
        assert result == "micro_probe"
        assert "Cold-start" in caplog.text

    @patch(
        "codeprobe.cli.mine_cmd._cold_start_check",
        return_value=True,
    )
    def test_general_falls_back_on_cold_start(self, _csc, tmp_path, caplog) -> None:
        with caplog.at_level(logging.WARNING):
            result = _resolve_task_type("mixed", tmp_path, "auto")
        assert result == "micro_probe"
        assert "Cold-start" in caplog.text

    @patch(
        "codeprobe.cli.mine_cmd._cold_start_check",
        return_value=True,
    )
    def test_sdlc_not_affected_by_cold_start(self, _csc, tmp_path) -> None:
        result = _resolve_task_type("sdlc_code_change", tmp_path, "auto")
        assert result == "sdlc_code_change"

    @patch(
        "codeprobe.cli.mine_cmd._cold_start_check",
        return_value=True,
    )
    def test_mcp_not_affected_by_cold_start(self, _csc, tmp_path) -> None:
        result = _resolve_task_type("mcp_tool_usage", tmp_path, "auto")
        assert result == "mcp_tool_usage"


# ---------------------------------------------------------------------------
# _resolve_task_type — comprehension generator fallback
# ---------------------------------------------------------------------------


class TestResolveTaskTypeComprehensionFallback:
    """When comprehension generator is absent, architecture_comprehension
    falls back to micro_probe."""

    @patch(
        "codeprobe.cli.mine_cmd._comprehension_generator_available",
        return_value=False,
    )
    @patch(
        "codeprobe.cli.mine_cmd._cold_start_check",
        return_value=False,
    )
    def test_comprehension_fallback_when_generator_missing(
        self, _csc, _cga, tmp_path, caplog
    ) -> None:
        with caplog.at_level(logging.WARNING):
            result = _resolve_task_type("architecture_comprehension", tmp_path, "auto")
        assert result == "micro_probe"
        assert "Comprehension generator not available" in caplog.text

    @patch(
        "codeprobe.cli.mine_cmd._comprehension_generator_available",
        return_value=True,
    )
    @patch(
        "codeprobe.cli.mine_cmd._cold_start_check",
        return_value=False,
    )
    def test_comprehension_passes_when_generator_available(
        self, _csc, _cga, tmp_path
    ) -> None:
        result = _resolve_task_type("architecture_comprehension", tmp_path, "auto")
        assert result == "architecture_comprehension"

    @patch(
        "codeprobe.cli.mine_cmd._cold_start_check",
        return_value=False,
    )
    def test_non_comprehension_types_unaffected(self, _csc, tmp_path) -> None:
        result = _resolve_task_type("sdlc_code_change", tmp_path, "auto")
        assert result == "sdlc_code_change"

    @patch(
        "codeprobe.cli.mine_cmd._cold_start_check",
        return_value=False,
    )
    def test_mixed_type_passes_through_with_merge_commits(self, _csc, tmp_path) -> None:
        result = _resolve_task_type("mixed", tmp_path, "auto")
        assert result == "mixed"


# ---------------------------------------------------------------------------
# --goal CLI flag integration (via run_mine)
# ---------------------------------------------------------------------------


class TestGoalFlag:
    """Verify --goal flag sets goal without interactive prompt."""

    @patch(
        "codeprobe.cli.mine_cmd._resolve_task_type",
        return_value="sdlc_code_change",
    )
    @patch("codeprobe.mining.mine_tasks")
    @patch("codeprobe.mining.write_task_dir")
    @patch("codeprobe.cli.mine_cmd._resolve_repo_path")
    def test_goal_quality_sets_sdlc_task_type(
        self, mock_resolve, mock_write, mock_mine, mock_rtt, tmp_path
    ) -> None:
        from codeprobe.cli.mine_cmd import run_mine

        mock_resolve.return_value = tmp_path
        mock_mine.return_value = MagicMock(tasks=[], pr_bodies={}, changed_files_map={})

        # Should not raise and should not call _ask_eval_goal
        with patch("codeprobe.cli.mine_cmd._ask_eval_goal") as mock_ask:
            run_mine(str(tmp_path), goal="quality", interactive=False)
            mock_ask.assert_not_called()

        # Verify _resolve_task_type was called with sdlc_code_change
        mock_rtt.assert_called_once()
        call_args = mock_rtt.call_args
        assert call_args[0][0] == "sdlc_code_change"

    @patch(
        "codeprobe.cli.mine_cmd._resolve_task_type",
        return_value="micro_probe",
    )
    @patch("codeprobe.mining.mine_tasks")
    @patch("codeprobe.mining.write_task_dir")
    @patch("codeprobe.cli.mine_cmd._resolve_repo_path")
    def test_goal_navigation_sets_architecture_comprehension(
        self, mock_resolve, mock_write, mock_mine, mock_rtt, tmp_path
    ) -> None:
        from codeprobe.cli.mine_cmd import run_mine

        mock_resolve.return_value = tmp_path
        mock_mine.return_value = MagicMock(tasks=[], pr_bodies={}, changed_files_map={})

        run_mine(str(tmp_path), goal="navigation", interactive=False)

        mock_rtt.assert_called_once()
        call_args = mock_rtt.call_args
        assert call_args[0][0] == "architecture_comprehension"

    def test_invalid_goal_raises_usage_error(self, tmp_path) -> None:
        import click

        from codeprobe.cli.mine_cmd import run_mine

        with patch(
            "codeprobe.cli.mine_cmd._resolve_repo_path",
            return_value=tmp_path,
        ):
            with patch(
                "codeprobe.cli.mine_cmd._resolve_task_type",
                return_value="mixed",
            ):
                with pytest.raises(click.UsageError, match="Unknown goal"):
                    run_mine(str(tmp_path), goal="invalid", interactive=False)

    @patch("codeprobe.cli.mine_cmd._resolve_task_type", return_value="mixed")
    @patch("codeprobe.mining.mine_tasks")
    @patch("codeprobe.mining.write_task_dir")
    @patch("codeprobe.cli.mine_cmd._resolve_repo_path")
    def test_goal_general_sets_mixed_task_type(
        self, mock_resolve, mock_write, mock_mine, mock_rtt, tmp_path
    ) -> None:
        from codeprobe.cli.mine_cmd import run_mine

        mock_resolve.return_value = tmp_path
        mock_mine.return_value = MagicMock(tasks=[], pr_bodies={}, changed_files_map={})

        run_mine(str(tmp_path), goal="general", interactive=False)

        mock_rtt.assert_called_once()
        call_args = mock_rtt.call_args
        assert call_args[0][0] == "mixed"


# ---------------------------------------------------------------------------
# _dispatch_by_task_type — routing to correct pipeline
# ---------------------------------------------------------------------------


class TestDispatchByTaskType:
    """Verify _dispatch_by_task_type routes to the correct generation pipeline."""

    @patch("codeprobe.cli.mine_cmd._dispatch_sdlc")
    def test_sdlc_code_change_routes_to_sdlc(self, mock_sdlc, tmp_path) -> None:
        from codeprobe.cli.mine_cmd import _dispatch_by_task_type

        _dispatch_by_task_type(
            task_type="sdlc_code_change",
            repo_path=tmp_path,
            count=5,
            source="auto",
            min_files=0,
            subsystems=(),
            no_llm=False,
            enrich=False,
            goal_name="Code quality comparison",
            bias="mixed",
        )
        mock_sdlc.assert_called_once()

    @patch("codeprobe.cli.mine_cmd._dispatch_sdlc")
    def test_mcp_tool_usage_routes_to_sdlc(self, mock_sdlc, tmp_path) -> None:
        from codeprobe.cli.mine_cmd import _dispatch_by_task_type

        _dispatch_by_task_type(
            task_type="mcp_tool_usage",
            repo_path=tmp_path,
            count=5,
            source="auto",
            min_files=6,
            subsystems=(),
            no_llm=False,
            enrich=False,
            goal_name="MCP / tool benefit",
            bias="hard",
        )
        mock_sdlc.assert_called_once()

    @patch("codeprobe.cli.mine_cmd._dispatch_probes")
    def test_micro_probe_routes_to_probes(self, mock_probes, tmp_path) -> None:
        from codeprobe.cli.mine_cmd import _dispatch_by_task_type

        _dispatch_by_task_type(
            task_type="micro_probe",
            repo_path=tmp_path,
            count=5,
            source="auto",
            min_files=0,
            subsystems=(),
            no_llm=False,
            enrich=False,
            goal_name="Codebase navigation",
            bias="mixed",
        )
        mock_probes.assert_called_once()

    @patch("codeprobe.cli.mine_cmd._dispatch_comprehension")
    def test_architecture_comprehension_routes_to_comprehension(
        self, mock_comp, tmp_path
    ) -> None:
        from codeprobe.cli.mine_cmd import _dispatch_by_task_type

        _dispatch_by_task_type(
            task_type="architecture_comprehension",
            repo_path=tmp_path,
            count=5,
            source="auto",
            min_files=0,
            subsystems=(),
            no_llm=False,
            enrich=False,
            goal_name="Codebase navigation",
            bias="mixed",
        )
        mock_comp.assert_called_once()

    @patch("codeprobe.cli.mine_cmd._dispatch_mixed")
    def test_mixed_routes_to_mixed(self, mock_mixed, tmp_path) -> None:
        from codeprobe.cli.mine_cmd import _dispatch_by_task_type

        _dispatch_by_task_type(
            task_type="mixed",
            repo_path=tmp_path,
            count=5,
            source="auto",
            min_files=0,
            subsystems=(),
            no_llm=False,
            enrich=False,
            goal_name="General benchmarking",
            bias="balanced",
        )
        mock_mixed.assert_called_once()

    @patch("codeprobe.cli.mine_cmd._dispatch_sdlc")
    def test_unknown_task_type_falls_back_to_sdlc(self, mock_sdlc, tmp_path) -> None:
        from codeprobe.cli.mine_cmd import _dispatch_by_task_type

        _dispatch_by_task_type(
            task_type="unknown_type",
            repo_path=tmp_path,
            count=5,
            source="auto",
            min_files=0,
            subsystems=(),
            no_llm=False,
            enrich=False,
            goal_name="Test",
            bias="mixed",
        )
        mock_sdlc.assert_called_once()


# ---------------------------------------------------------------------------
# Dispatch pipeline integration — verify each pipeline is callable
# ---------------------------------------------------------------------------


class TestDispatchPipelineIntegration:
    """Verify dispatch pipelines call expected generators."""

    @patch("codeprobe.cli.mine_cmd._show_next_steps")
    @patch("codeprobe.cli.mine_cmd._record_task_ids_in_experiment")
    @patch("codeprobe.cli.mine_cmd._clear_tasks_dir")
    @patch("codeprobe.cli.mine_cmd._enrich_sdlc_tasks")
    @patch("codeprobe.mining.write_task_dir")
    @patch("codeprobe.mining.mine_tasks")
    def test_dispatch_sdlc_calls_mine_tasks(
        self,
        mock_mine,
        mock_write,
        mock_enrich,
        mock_clear,
        mock_record,
        mock_next,
        tmp_path,
    ) -> None:
        from codeprobe.cli.mine_cmd import _dispatch_sdlc

        mock_task = MagicMock()
        mock_task.id = "task-001"
        mock_task.metadata.difficulty = "medium"
        mock_task.metadata.language = "python"
        mock_task.metadata.quality_score = 0.8
        mock_task.metadata.description = "A test task with enough description"
        mock_task.verification.command = "bash tests/test.sh"
        mock_mine.return_value = MagicMock(
            tasks=[mock_task], pr_bodies={}, changed_files_map={}
        )
        mock_enrich.return_value = [mock_task]
        mock_clear.return_value = tmp_path / "tasks"

        _dispatch_sdlc(
            repo_path=tmp_path,
            count=5,
            source="auto",
            min_files=0,
            subsystems=(),
            no_llm=False,
            enrich=False,
            goal_name="Code quality",
            bias="mixed",
        )
        mock_mine.assert_called_once()

    @patch("codeprobe.cli.mine_cmd._show_next_steps")
    @patch("codeprobe.cli.mine_cmd._record_task_ids_in_experiment")
    @patch("codeprobe.cli.mine_cmd._clear_tasks_dir")
    @patch("codeprobe.probe.adapter.ProbeTaskAdapter.convert_batch")
    @patch("codeprobe.cli.mine_cmd.generate_probes", create=True)
    def test_dispatch_probes_calls_generate_probes(
        self,
        mock_gen,
        mock_batch,
        mock_clear,
        mock_record,
        mock_next,
        tmp_path,
    ) -> None:
        from codeprobe.cli.mine_cmd import _dispatch_probes

        # Patch at the point of import inside _dispatch_probes
        mock_probe = MagicMock()
        mock_probe.template_name = "find_function"
        with patch(
            "codeprobe.probe.generator.generate_probes", return_value=[mock_probe]
        ):
            mock_clear.return_value = tmp_path / "tasks"
            task_dir = tmp_path / "tasks" / "probe-findfunction-000"
            task_dir.mkdir(parents=True)
            mock_batch.return_value = [task_dir]

            _dispatch_probes(
                repo_path=tmp_path,
                count=5,
                goal_name="Navigation",
                bias="mixed",
            )
            mock_batch.assert_called_once()

    @patch("codeprobe.cli.mine_cmd._show_next_steps")
    @patch("codeprobe.cli.mine_cmd._record_task_ids_in_experiment")
    @patch("codeprobe.cli.mine_cmd._clear_tasks_dir")
    @patch("codeprobe.cli.mine_cmd._show_results_table")
    @patch("codeprobe.mining.writer.write_task_dir")
    def test_dispatch_comprehension_calls_generator(
        self,
        mock_write,
        mock_table,
        mock_clear,
        mock_record,
        mock_next,
        tmp_path,
    ) -> None:
        from codeprobe.cli.mine_cmd import _dispatch_comprehension

        mock_task = MagicMock()
        mock_task.id = "comp-001"
        mock_task.metadata.difficulty = "hard"
        mock_task.metadata.language = "python"
        mock_task.metadata.quality_score = 0.9
        mock_task.metadata.description = "Comprehension task"
        mock_task.verification.command = "bash tests/test.sh"

        mock_clear.return_value = tmp_path / "tasks"

        with patch("codeprobe.mining.comprehension.ComprehensionGenerator") as MockGen:
            instance = MockGen.return_value
            instance.generate.return_value = [mock_task]

            _dispatch_comprehension(
                repo_path=tmp_path,
                count=5,
                goal_name="Navigation",
                bias="mixed",
            )
            instance.generate.assert_called_once_with(count=5)
