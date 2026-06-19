"""Tests for the ``codeprobe models list`` command (codeprobe-8yjf)."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from codeprobe.cli import main


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_models_list_all_agents(runner: CliRunner) -> None:
    result = runner.invoke(main, ["models", "list"])
    assert result.exit_code == 0, result.output
    # Every registered agent appears.
    assert "claude" in result.output
    assert "codex" in result.output
    assert "copilot" in result.output


def test_models_list_claude_shows_aliases_and_ids(runner: CliRunner) -> None:
    result = runner.invoke(main, ["models", "list", "--agent", "claude"])
    assert result.exit_code == 0, result.output
    assert "sonnet" in result.output
    assert "claude-sonnet-4-6" in result.output
    assert "default:" in result.output


def test_models_list_advisory_marked(runner: CliRunner) -> None:
    result = runner.invoke(main, ["models", "list", "--agent", "codex"])
    assert result.exit_code == 0, result.output
    assert "advisory" in result.output


def test_models_list_unknown_agent_errors(runner: CliRunner) -> None:
    result = runner.invoke(main, ["models", "list", "--agent", "bogus"])
    assert result.exit_code != 0
    assert "bogus" in result.output
