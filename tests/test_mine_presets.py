"""Tests for deprecated --preset alias and legacy preset translation."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from codeprobe.cli.mine_cmd import _PRESET_ALIASES

# ---------------------------------------------------------------------------
# _PRESET_ALIASES structure
# ---------------------------------------------------------------------------


class TestPresetAliases:
    """The legacy --preset flag is a deprecated alias mapping to a goal."""

    def test_quick_aliases_to_general_with_count_3(self) -> None:
        goal, overrides = _PRESET_ALIASES["quick"]
        assert goal == "general"
        assert overrides == {"count": 3}

    def test_mcp_aliases_to_goal_mcp(self) -> None:
        goal, overrides = _PRESET_ALIASES["mcp"]
        assert goal == "mcp"
        assert overrides == {}

    def test_only_two_aliases_exist(self) -> None:
        assert set(_PRESET_ALIASES.keys()) == {"quick", "mcp"}


# ---------------------------------------------------------------------------
# MCP discovery importable from core (preserved from old file)
# ---------------------------------------------------------------------------


class TestMcpDiscoveryImport:
    """Verify _discover_mcp_configs is importable from core.mcp_discovery."""

    def test_importable(self) -> None:
        from codeprobe.core.mcp_discovery import discover_mcp_configs

        assert callable(discover_mcp_configs)

    def test_search_paths_importable(self) -> None:
        from codeprobe.core.mcp_discovery import MCP_SEARCH_PATHS

        assert isinstance(MCP_SEARCH_PATHS, list)
        assert len(MCP_SEARCH_PATHS) > 0


# ---------------------------------------------------------------------------
# --preset CLI flag: still accepted, emits deprecation warning, translates
# ---------------------------------------------------------------------------


def _make_mine_runner_mocks():
    """Return a stack of context-manager patches that make mine() safely callable."""
    return [
        patch("codeprobe.cli.mine_cmd._resolve_repo_path"),
        patch("codeprobe.cli.mine_cmd._run_org_scale_mine"),
        patch("codeprobe.cli.mine_cmd._dispatch_by_task_type"),
        patch("codeprobe.cli.mine_cmd._resolve_task_type", return_value="mixed"),
    ]


class TestPresetCLIDeprecation:
    """The --preset CLI flag still works but is deprecated."""

    def test_preset_quick_translates_to_general_count_3(self, tmp_path) -> None:
        from codeprobe.cli import main

        runner = CliRunner()
        with (
            patch("codeprobe.cli.mine_cmd._resolve_repo_path", return_value=tmp_path),
            patch("codeprobe.cli.mine_cmd._run_org_scale_mine"),
            patch("codeprobe.cli.mine_cmd._dispatch_by_task_type") as mock_dispatch,
            patch("codeprobe.cli.mine_cmd._resolve_task_type", return_value="mixed"),
        ):
            result = runner.invoke(
                main,
                ["mine", "--preset", "quick", "--no-interactive", str(tmp_path)],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "deprecated" in result.output.lower()
        mock_dispatch.assert_called_once()
        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs["count"] == 3

    def test_preset_mcp_translates_to_goal_mcp(self, tmp_path) -> None:
        from codeprobe.cli import main

        runner = CliRunner()
        with (
            patch("codeprobe.cli.mine_cmd._resolve_repo_path", return_value=tmp_path),
            patch("codeprobe.cli.mine_cmd._run_org_scale_mine") as mock_org,
            patch("codeprobe.cli.mine_cmd._dispatch_by_task_type") as mock_dispatch,
            patch("codeprobe.cli.mine_cmd._resolve_task_type", return_value="mixed"),
        ):
            result = runner.invoke(
                main,
                ["mine", "--preset", "mcp", "--no-interactive", str(tmp_path)],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "deprecated" in result.output.lower()
        # mcp goal expands to org_scale=True → should dispatch to _run_org_scale_mine,
        # not _dispatch_by_task_type
        mock_org.assert_called_once()
        mock_dispatch.assert_not_called()

    def test_preset_quick_with_explicit_count_keeps_explicit(self, tmp_path) -> None:
        """--preset quick --count 7 keeps count=7, not 3."""
        from codeprobe.cli import main

        runner = CliRunner()
        with (
            patch("codeprobe.cli.mine_cmd._resolve_repo_path", return_value=tmp_path),
            patch("codeprobe.cli.mine_cmd._dispatch_by_task_type") as mock_dispatch,
            patch("codeprobe.cli.mine_cmd._resolve_task_type", return_value="mixed"),
        ):
            result = runner.invoke(
                main,
                [
                    "mine",
                    "--preset",
                    "quick",
                    "--count",
                    "7",
                    "--no-interactive",
                    str(tmp_path),
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        mock_dispatch.assert_called_once()
        assert mock_dispatch.call_args.kwargs["count"] == 7

    def test_preset_and_goal_conflict_raises(self, tmp_path) -> None:
        """--preset mcp --goal quality raises UsageError."""
        from codeprobe.cli import main

        runner = CliRunner()
        with (
            patch("codeprobe.cli.mine_cmd._resolve_repo_path", return_value=tmp_path),
        ):
            result = runner.invoke(
                main,
                [
                    "mine",
                    "--preset",
                    "mcp",
                    "--goal",
                    "quality",
                    "--no-interactive",
                    str(tmp_path),
                ],
            )

        assert result.exit_code != 0
        assert "preset" in result.output.lower() and "goal" in result.output.lower()

    def test_preset_and_goal_same_value_is_ok(self, tmp_path) -> None:
        """--preset mcp --goal mcp is fine (redundant but not conflicting)."""
        from codeprobe.cli import main

        runner = CliRunner()
        with (
            patch("codeprobe.cli.mine_cmd._resolve_repo_path", return_value=tmp_path),
            patch("codeprobe.cli.mine_cmd._run_org_scale_mine") as mock_org,
            patch("codeprobe.cli.mine_cmd._dispatch_by_task_type"),
            patch("codeprobe.cli.mine_cmd._resolve_task_type", return_value="mixed"),
        ):
            result = runner.invoke(
                main,
                [
                    "mine",
                    "--preset",
                    "mcp",
                    "--goal",
                    "mcp",
                    "--no-interactive",
                    str(tmp_path),
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        mock_org.assert_called_once()

    def test_preset_invalid_rejected(self) -> None:
        from codeprobe.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["mine", "--preset", "invalid", "."])
        assert result.exit_code != 0
        assert "Invalid value" in result.output or "invalid" in result.output.lower()


# ---------------------------------------------------------------------------
# Profile × CLI goal/preset interaction
# ---------------------------------------------------------------------------


class TestProfileGoalPresetInteraction:
    """Profile-loaded goal/preset must not conflict with CLI-explicit ones.

    Rule: if the user explicitly passed --goal OR --preset on the CLI, any
    goal/preset value from a profile is ignored. Explicit CLI > profile.
    """

    def test_cli_goal_overrides_profile_preset(self, tmp_path) -> None:
        """profile{preset:mcp} + CLI --goal quality → CLI wins, no error."""
        from codeprobe.cli import main

        runner = CliRunner()
        with (
            patch(
                "codeprobe.cli.mine_cmd.load_profile",
                return_value={"preset": "mcp"},
            ),
            patch("codeprobe.cli.mine_cmd._resolve_repo_path", return_value=tmp_path),
            patch("codeprobe.cli.mine_cmd._run_org_scale_mine") as mock_org,
            patch("codeprobe.cli.mine_cmd._dispatch_by_task_type") as mock_dispatch,
            patch(
                "codeprobe.cli.mine_cmd._resolve_task_type",
                return_value="sdlc_code_change",
            ),
        ):
            result = runner.invoke(
                main,
                [
                    "mine",
                    "--profile",
                    "p",
                    "--goal",
                    "quality",
                    "--no-interactive",
                    str(tmp_path),
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        mock_dispatch.assert_called_once()
        mock_org.assert_not_called()

    def test_cli_preset_overrides_profile_goal(self, tmp_path) -> None:
        """profile{goal:quality} + CLI --preset mcp → CLI wins, warns."""
        from codeprobe.cli import main

        runner = CliRunner()
        with (
            patch(
                "codeprobe.cli.mine_cmd.load_profile",
                return_value={"goal": "quality"},
            ),
            patch("codeprobe.cli.mine_cmd._resolve_repo_path", return_value=tmp_path),
            patch("codeprobe.cli.mine_cmd._run_org_scale_mine") as mock_org,
            patch("codeprobe.cli.mine_cmd._dispatch_by_task_type") as mock_dispatch,
            patch("codeprobe.cli.mine_cmd._resolve_task_type", return_value="mixed"),
        ):
            result = runner.invoke(
                main,
                [
                    "mine",
                    "--profile",
                    "p",
                    "--preset",
                    "mcp",
                    "--no-interactive",
                    str(tmp_path),
                ],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        assert "deprecated" in result.output.lower()
        mock_org.assert_called_once()
        mock_dispatch.assert_not_called()

    def test_profile_goal_applied_when_no_cli_goal_or_preset(self, tmp_path) -> None:
        """profile{goal:mcp} with nothing on CLI → profile goal wins."""
        from codeprobe.cli import main

        runner = CliRunner()
        with (
            patch(
                "codeprobe.cli.mine_cmd.load_profile",
                return_value={"goal": "mcp"},
            ),
            patch("codeprobe.cli.mine_cmd._resolve_repo_path", return_value=tmp_path),
            patch("codeprobe.cli.mine_cmd._run_org_scale_mine") as mock_org,
            patch("codeprobe.cli.mine_cmd._resolve_task_type", return_value="mixed"),
        ):
            result = runner.invoke(
                main,
                ["mine", "--profile", "p", "--no-interactive", str(tmp_path)],
                catch_exceptions=False,
            )

        assert result.exit_code == 0, result.output
        mock_org.assert_called_once()
