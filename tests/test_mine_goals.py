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
