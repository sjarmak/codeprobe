"""Tests for eval goal mappings, --goal flag, and cold-start fallback."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from codeprobe.cli.mine_cmd import (
    _EVAL_GOALS,
    _NUMERIC_GOAL_KEYS,
    _resolve_task_type,
)

# ---------------------------------------------------------------------------
# _EVAL_GOALS structure (dict-of-dicts keyed by flag name)
# ---------------------------------------------------------------------------


class TestEvalGoals:
    """Verify _EVAL_GOALS is keyed by flag name and carries name/bias/task_type/extras."""

    _REQUIRED_KEYS = {"name", "bias", "task_type", "extras"}

    def test_keys_are_flag_names(self) -> None:
        assert set(_EVAL_GOALS.keys()) == {"quality", "navigation", "mcp", "general"}

    def test_each_entry_has_required_keys(self) -> None:
        for name, entry in _EVAL_GOALS.items():
            missing = self._REQUIRED_KEYS - set(entry.keys())
            assert not missing, f"Goal {name!r} missing keys: {missing}"
            assert isinstance(entry["extras"], dict)

    def test_quality_goal(self) -> None:
        g = _EVAL_GOALS["quality"]
        assert g["name"] == "Code quality comparison"
        assert g["bias"] == "mixed"
        assert g["task_type"] == "sdlc_code_change"
        # Quality goal enables enrichment and bumps min_files to 2 via extras
        assert g["extras"].get("enrich") is True
        assert g["extras"].get("min_files") == 2

    def test_navigation_goal(self) -> None:
        g = _EVAL_GOALS["navigation"]
        assert g["name"] == "Codebase navigation"
        assert g["bias"] == "mixed"
        assert g["task_type"] == "architecture_comprehension"
        assert g["extras"] == {}

    def test_mcp_goal_expands_to_full_config(self) -> None:
        """The mcp goal must set org_scale, mcp_families, enrich, count=8,
        and min_files=6 via extras.

        This is the bug fix: previously the mcp 'goal' and mcp 'preset' had
        different behaviors. The goal now owns the full configuration.
        """
        g = _EVAL_GOALS["mcp"]
        assert g["name"] == "MCP / tool benefit"
        assert g["bias"] == "hard"
        assert g["task_type"] == "mcp_tool_usage"
        assert g["extras"]["org_scale"] is True
        assert g["extras"]["mcp_families"] is True
        assert g["extras"]["enrich"] is True
        assert g["extras"]["count"] == 8
        assert g["extras"]["min_files"] == 6

    def test_general_goal(self) -> None:
        g = _EVAL_GOALS["general"]
        assert g["name"] == "General benchmarking"
        assert g["bias"] == "balanced"
        assert g["task_type"] == "mixed"
        assert g["extras"] == {}


# ---------------------------------------------------------------------------
# _NUMERIC_GOAL_KEYS — interactive prompt mapping
# ---------------------------------------------------------------------------


class TestNumericGoalKeys:
    """The interactive _ask_eval_goal prompt uses numeric keys (1-4) that
    must map back to the dict-keyed goal names."""

    def test_maps_to_all_goals(self) -> None:
        assert _NUMERIC_GOAL_KEYS["1"] == "quality"
        assert _NUMERIC_GOAL_KEYS["2"] == "navigation"
        assert _NUMERIC_GOAL_KEYS["3"] == "mcp"
        assert _NUMERIC_GOAL_KEYS["4"] == "general"

    def test_all_numeric_keys_resolve(self) -> None:
        for num, goal_name in _NUMERIC_GOAL_KEYS.items():
            assert goal_name in _EVAL_GOALS


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
            tasks=[mock_task],
            pr_bodies={},
            changed_files_map={},
            min_files_used=0,
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


# ---------------------------------------------------------------------------
# Goal extras expansion — the core behavior fix
# ---------------------------------------------------------------------------


class TestGoalExtrasExpansion:
    """Goals expand to a set of defaults that override Click defaults
    (but never explicit CLI values)."""

    def test_goal_mcp_dispatches_to_org_scale_branch(self, tmp_path) -> None:
        """The critical fix: --goal mcp now triggers org-scale mining
        because its extras set org_scale=True, mcp_families=True, enrich=True."""
        from codeprobe.cli.mine_cmd import run_mine

        with (
            patch(
                "codeprobe.cli.mine_cmd._resolve_repo_path",
                return_value=tmp_path,
            ),
            patch("codeprobe.cli.mine_cmd._run_org_scale_mine") as mock_org,
            patch("codeprobe.cli.mine_cmd._dispatch_by_task_type") as mock_dispatch,
            patch("codeprobe.cli.mine_cmd._resolve_task_type", return_value="mixed"),
        ):
            run_mine(str(tmp_path), goal="mcp", interactive=False)

        mock_org.assert_called_once()
        mock_dispatch.assert_not_called()
        # org-scale count comes from mcp goal extras (count=8)
        assert mock_org.call_args.kwargs["count"] == 8
        assert mock_org.call_args.kwargs["mcp_families"] is True

    def test_goal_mcp_explicit_count_override(self, tmp_path) -> None:
        """--goal mcp --count 2 must keep count=2, not use the goal's count=8."""
        from codeprobe.cli.mine_cmd import run_mine

        with (
            patch(
                "codeprobe.cli.mine_cmd._resolve_repo_path",
                return_value=tmp_path,
            ),
            patch("codeprobe.cli.mine_cmd._run_org_scale_mine") as mock_org,
            patch("codeprobe.cli.mine_cmd._resolve_task_type", return_value="mixed"),
        ):
            run_mine(
                str(tmp_path),
                goal="mcp",
                count=2,
                interactive=False,
                explicit_set=frozenset({"count"}),
            )

        mock_org.assert_called_once()
        assert mock_org.call_args.kwargs["count"] == 2

    def test_goal_quality_enables_enrich(self, tmp_path) -> None:
        """The quality goal sets enrich=True via extras."""
        from codeprobe.cli.mine_cmd import run_mine

        with (
            patch(
                "codeprobe.cli.mine_cmd._resolve_repo_path",
                return_value=tmp_path,
            ),
            patch("codeprobe.cli.mine_cmd._dispatch_by_task_type") as mock_dispatch,
            patch(
                "codeprobe.cli.mine_cmd._resolve_task_type",
                return_value="sdlc_code_change",
            ),
        ):
            run_mine(str(tmp_path), goal="quality", interactive=False)

        mock_dispatch.assert_called_once()
        assert mock_dispatch.call_args.kwargs["enrich"] is True

    def test_goal_general_no_extras(self, tmp_path) -> None:
        """The general goal has no extras; explicit defaults remain unchanged."""
        from codeprobe.cli.mine_cmd import run_mine

        with (
            patch(
                "codeprobe.cli.mine_cmd._resolve_repo_path",
                return_value=tmp_path,
            ),
            patch("codeprobe.cli.mine_cmd._dispatch_by_task_type") as mock_dispatch,
            patch("codeprobe.cli.mine_cmd._resolve_task_type", return_value="mixed"),
        ):
            run_mine(str(tmp_path), goal="general", interactive=False)

        mock_dispatch.assert_called_once()
        # enrich and others stay at their defaults
        assert mock_dispatch.call_args.kwargs["enrich"] is False

    def test_non_default_value_not_in_explicit_set_still_blocks_extras(
        self, tmp_path
    ) -> None:
        """If a caller passes count=10 directly (simulating a profile override),
        goal extras must not overwrite it even though explicit_set is empty."""
        from codeprobe.cli.mine_cmd import run_mine

        with (
            patch(
                "codeprobe.cli.mine_cmd._resolve_repo_path",
                return_value=tmp_path,
            ),
            patch("codeprobe.cli.mine_cmd._run_org_scale_mine") as mock_org,
            patch("codeprobe.cli.mine_cmd._resolve_task_type", return_value="mixed"),
        ):
            # count=10 is non-default and came from a profile (explicit_set empty).
            # The mcp goal's count=8 must NOT override a profile-set value.
            run_mine(str(tmp_path), goal="mcp", count=10, interactive=False)

        mock_org.assert_called_once()
        assert mock_org.call_args.kwargs["count"] == 10

    def test_explicit_min_files_zero_is_respected(self, tmp_path) -> None:
        """--goal quality --min-files 0 must keep min_files=0, not 2.

        Regression for the bug where `if min_files == 0: min_files = goal_default`
        silently overrode an explicit 0.
        """
        from codeprobe.cli.mine_cmd import run_mine

        with (
            patch(
                "codeprobe.cli.mine_cmd._resolve_repo_path",
                return_value=tmp_path,
            ),
            patch("codeprobe.cli.mine_cmd._dispatch_by_task_type") as mock_dispatch,
            patch(
                "codeprobe.cli.mine_cmd._resolve_task_type",
                return_value="sdlc_code_change",
            ),
        ):
            run_mine(
                str(tmp_path),
                goal="quality",
                min_files=0,
                interactive=False,
                explicit_set=frozenset({"min_files"}),
            )

        mock_dispatch.assert_called_once()
        assert mock_dispatch.call_args.kwargs["min_files"] == 0

    def test_profile_min_files_zero_is_respected(self, tmp_path) -> None:
        """Profile-set min_files=0 must survive goal extras."""
        from codeprobe.cli.mine_cmd import run_mine

        with (
            patch(
                "codeprobe.cli.mine_cmd._resolve_repo_path",
                return_value=tmp_path,
            ),
            patch("codeprobe.cli.mine_cmd._dispatch_by_task_type") as mock_dispatch,
            patch(
                "codeprobe.cli.mine_cmd._resolve_task_type",
                return_value="sdlc_code_change",
            ),
        ):
            run_mine(
                str(tmp_path),
                goal="quality",
                min_files=0,
                interactive=False,
                profile_set=frozenset({"min_files"}),
            )

        mock_dispatch.assert_called_once()
        assert mock_dispatch.call_args.kwargs["min_files"] == 0

    def test_goal_quality_default_min_files_from_extras(self, tmp_path) -> None:
        """Without an explicit min_files, --goal quality sets min_files=2
        via its extras."""
        from codeprobe.cli.mine_cmd import run_mine

        with (
            patch(
                "codeprobe.cli.mine_cmd._resolve_repo_path",
                return_value=tmp_path,
            ),
            patch("codeprobe.cli.mine_cmd._dispatch_by_task_type") as mock_dispatch,
            patch(
                "codeprobe.cli.mine_cmd._resolve_task_type",
                return_value="sdlc_code_change",
            ),
        ):
            run_mine(str(tmp_path), goal="quality", interactive=False)

        mock_dispatch.assert_called_once()
        assert mock_dispatch.call_args.kwargs["min_files"] == 2


# ---------------------------------------------------------------------------
# resolve_effective_config — pure helper covering precedence rules
# ---------------------------------------------------------------------------


class TestResolveEffectiveConfig:
    """Direct tests of the resolve_effective_config helper."""

    def _resolve(self, **overrides):
        from codeprobe.cli.mine_cmd import resolve_effective_config

        base = dict(
            goal=None,
            preset=None,
            count=5,
            source="auto",
            min_files=0,
            enrich=False,
            org_scale=False,
            mcp_families=False,
            explicit_set=frozenset(),
            profile_set=frozenset(),
            warn=None,
        )
        base.update(overrides)
        return resolve_effective_config(**base)

    def test_no_goal_no_preset_returns_inputs(self) -> None:
        result = self._resolve()
        assert result["goal"] is None
        assert result["count"] == 5
        assert result["org_scale"] is False

    def test_goal_mcp_expands_extras(self) -> None:
        result = self._resolve(goal="mcp")
        assert result["goal"] == "mcp"
        assert result["count"] == 8
        assert result["org_scale"] is True
        assert result["mcp_families"] is True
        assert result["enrich"] is True

    def test_explicit_count_blocks_goal_override(self) -> None:
        result = self._resolve(goal="mcp", count=2, explicit_set=frozenset({"count"}))
        assert result["count"] == 2
        # other extras still apply
        assert result["org_scale"] is True

    def test_non_default_count_blocks_goal_override(self) -> None:
        """Profile-set value (non-default, not in explicit_set) still blocks extras."""
        result = self._resolve(goal="mcp", count=10)
        assert result["count"] == 10
        assert result["org_scale"] is True  # still applied

    def test_preset_quick_translates_to_general(self) -> None:
        result = self._resolve(preset="quick")
        assert result["goal"] == "general"
        assert result["count"] == 3

    def test_preset_mcp_translates_to_goal_mcp(self) -> None:
        result = self._resolve(preset="mcp")
        assert result["goal"] == "mcp"
        assert result["count"] == 8
        assert result["org_scale"] is True

    def test_preset_and_different_goal_raises(self) -> None:
        import click

        with pytest.raises(click.UsageError, match="preset"):
            self._resolve(preset="mcp", goal="quality")

    def test_preset_and_matching_goal_ok(self) -> None:
        """--preset mcp --goal mcp is redundant but not an error."""
        result = self._resolve(preset="mcp", goal="mcp")
        assert result["goal"] == "mcp"
        assert result["org_scale"] is True

    def test_invalid_goal_raises(self) -> None:
        import click

        with pytest.raises(click.UsageError, match="Unknown goal"):
            self._resolve(goal="bogus")

    def test_invalid_preset_raises(self) -> None:
        import click

        with pytest.raises(click.UsageError, match="preset"):
            self._resolve(preset="bogus")

    def test_preset_emits_deprecation_warning(self) -> None:
        messages: list[str] = []
        self._resolve(preset="mcp", warn=lambda msg: messages.append(msg))
        assert len(messages) == 1
        assert "deprecated" in messages[0].lower()

    def test_goal_mcp_without_preset_no_warning(self) -> None:
        messages: list[str] = []
        self._resolve(goal="mcp", warn=lambda msg: messages.append(msg))
        assert messages == []

    def test_profile_set_blocks_goal_extras(self) -> None:
        """profile_set behaves like explicit_set for goal-extra overrides."""
        result = self._resolve(goal="mcp", count=10, profile_set=frozenset({"count"}))
        assert result["count"] == 10
        # Non-profile-set keys still get the goal extras.
        assert result["org_scale"] is True

    def test_explicit_set_beats_profile_set_symmetrically(self) -> None:
        """Both sets equally protect against goal extras; CLI and profile
        are distinct layers but collapse to 'protected' for extras blocking."""
        result = self._resolve(
            goal="mcp",
            count=2,
            explicit_set=frozenset({"count"}),
            profile_set=frozenset({"count"}),
        )
        assert result["count"] == 2

    def test_profile_min_files_zero_blocks_goal_extras(self) -> None:
        """min_files=0 via profile_set must not be overridden by goal extras."""
        result = self._resolve(
            goal="quality", min_files=0, profile_set=frozenset({"min_files"})
        )
        assert result["min_files"] == 0

    def test_explicit_min_files_zero_blocks_goal_extras(self) -> None:
        """min_files=0 via explicit_set must not be overridden by goal extras."""
        result = self._resolve(
            goal="quality", min_files=0, explicit_set=frozenset({"min_files"})
        )
        assert result["min_files"] == 0
