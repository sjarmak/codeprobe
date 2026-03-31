"""Tests for CLI entry point."""

from click.testing import CliRunner

from codeprobe.cli import main


def test_cli_version():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "codeprobe" in result.output
    assert "0.1.0a1" in result.output


def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Benchmark AI coding agents" in result.output


def test_cli_commands_registered():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    for cmd in ("init", "mine", "run", "interpret", "assess"):
        assert cmd in result.output, f"Command '{cmd}' not found in help output"


def test_cli_run_has_max_cost_option():
    runner = CliRunner()
    result = runner.invoke(main, ["run", "--help"])
    assert result.exit_code == 0
    assert "--max-cost-usd" in result.output
    assert "CODEPROBE_MAX_COST_USD" in result.output
