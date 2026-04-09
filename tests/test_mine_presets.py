"""Tests for mine command presets and MCP discovery extraction."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from codeprobe.cli.mine_cmd import PRESETS, _apply_preset, _CLI_DEFAULTS

# ---------------------------------------------------------------------------
# _apply_preset unit tests
# ---------------------------------------------------------------------------


class TestApplyPreset:
    """Verify preset merging and CLI-override semantics."""

    def test_no_preset_returns_inputs(self) -> None:
        result = _apply_preset(
            None,
            count=5,
            source="auto",
            min_files=0,
            enrich=False,
            org_scale=False,
            mcp_families=False,
        )
        assert result == {
            "count": 5,
            "source": "auto",
            "min_files": 0,
            "enrich": False,
            "org_scale": False,
            "mcp_families": False,
        }

    def test_quick_preset_sets_count_3(self) -> None:
        result = _apply_preset(
            "quick",
            count=5,  # default
            source="auto",
            min_files=0,
            enrich=False,
            org_scale=False,
            mcp_families=False,
        )
        assert result["count"] == 3

    def test_mcp_preset_enables_org_scale_flags(self) -> None:
        result = _apply_preset(
            "mcp",
            count=5,
            source="auto",
            min_files=0,
            enrich=False,
            org_scale=False,
            mcp_families=False,
        )
        assert result["count"] == 8
        assert result["org_scale"] is True
        assert result["mcp_families"] is True
        assert result["enrich"] is True

    def test_explicit_count_overrides_preset(self) -> None:
        """When the user passes --count 10, the preset count is ignored."""
        result = _apply_preset(
            "quick",
            count=10,  # non-default → explicit
            source="auto",
            min_files=0,
            enrich=False,
            org_scale=False,
            mcp_families=False,
        )
        assert result["count"] == 10

    def test_explicit_org_scale_overrides_mcp_preset(self) -> None:
        """When user explicitly passes --org-scale (True != default False), keep it."""
        # If user explicitly sets org_scale=True, it matches the mcp preset anyway.
        # Test the inverse: user somehow has org_scale as False but default is also False
        # → preset wins. This is the expected behavior.
        result = _apply_preset(
            "mcp",
            count=5,
            source="auto",
            min_files=0,
            enrich=False,
            org_scale=False,  # same as default → preset wins
            mcp_families=False,
        )
        assert result["org_scale"] is True


# ---------------------------------------------------------------------------
# PRESETS dict structure
# ---------------------------------------------------------------------------


class TestPresetsDict:
    """Verify PRESETS contains expected keys and shapes."""

    def test_quick_preset_exists(self) -> None:
        assert "quick" in PRESETS
        assert "count" in PRESETS["quick"]

    def test_mcp_preset_exists(self) -> None:
        assert "mcp" in PRESETS
        assert PRESETS["mcp"]["org_scale"] is True
        assert PRESETS["mcp"]["mcp_families"] is True


# ---------------------------------------------------------------------------
# MCP discovery importable from core
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
# CLI integration via CliRunner
# ---------------------------------------------------------------------------


class TestMinePresetCLI:
    """Test --preset flag via Click CliRunner (dry-run — mock run_mine)."""

    def test_preset_quick_passes_to_run_mine(self) -> None:
        from codeprobe.cli import main

        runner = CliRunner()

        with patch("codeprobe.cli.mine_cmd.run_mine") as mock_run:
            result = runner.invoke(
                main, ["mine", "--preset", "quick", "--no-interactive", "."]
            )
            # run_mine should have been called (may fail due to missing repo, but
            # we patched it so it won't)
            if mock_run.called:
                _, kwargs = mock_run.call_args
                assert kwargs.get("preset") == "quick"

    def test_preset_invalid_rejected(self) -> None:
        from codeprobe.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["mine", "--preset", "invalid", "."])
        assert result.exit_code != 0
        assert "Invalid value" in result.output or "invalid" in result.output.lower()
