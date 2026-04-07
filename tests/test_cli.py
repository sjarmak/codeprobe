"""Tests for CLI entry point."""

from click.testing import CliRunner

from codeprobe import __version__
from codeprobe.cli import main


def test_cli_version():
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert "codeprobe" in result.output
    assert __version__ in result.output


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


def test_main_module_runnable():
    """Verify `python -m codeprobe --version` works (needed for pipx run)."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-m", "codeprobe", "--version"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0
    assert __version__ in result.stdout


def test_cli_verbose_flag_sets_debug_level():
    import logging

    from codeprobe.cli import _configure_logging

    _configure_logging(verbose=1, quiet=False)
    assert logging.getLogger("codeprobe").level == logging.DEBUG


def test_cli_quiet_flag_sets_warning_level():
    import logging

    from codeprobe.cli import _configure_logging

    _configure_logging(verbose=0, quiet=True)
    assert logging.getLogger("codeprobe").level == logging.WARNING


def test_cli_default_is_info_level():
    import logging

    from codeprobe.cli import _configure_logging

    _configure_logging(verbose=0, quiet=False)
    assert logging.getLogger("codeprobe").level == logging.INFO


def test_logger_does_not_pollute_third_party():
    import logging

    from codeprobe.cli import _configure_logging

    httpx_level_before = logging.getLogger("httpx").level
    _configure_logging(verbose=1, quiet=False)
    assert logging.getLogger("httpx").level == httpx_level_before


def test_logger_writes_to_stderr_only():
    import logging
    import sys

    from codeprobe.cli import _configure_logging

    _configure_logging(verbose=0, quiet=False)
    logger = logging.getLogger("codeprobe")
    # Verify handler writes to stderr, not stdout
    assert len(logger.handlers) == 1
    assert logger.handlers[0].stream is sys.stderr


def test_logger_no_duplicate_handlers_on_repeat_invocation():
    import logging

    from codeprobe.cli import _configure_logging

    _configure_logging(verbose=0, quiet=False)
    _configure_logging(verbose=1, quiet=False)
    assert len(logging.getLogger("codeprobe").handlers) == 1


def test_main_help_lists_v_and_q_flags():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "-v" in result.output or "--verbose" in result.output
    assert "-q" in result.output or "--quiet" in result.output


def test_cli_run_has_max_cost_option():
    runner = CliRunner()
    result = runner.invoke(main, ["run", "--help"])
    assert result.exit_code == 0
    assert "--max-cost-usd" in result.output
    assert "CODEPROBE_MAX_COST_USD" in result.output


# ---------------------------------------------------------------------------
# --log-format option (bead 6)
# ---------------------------------------------------------------------------


def _reset_codeprobe_logger() -> None:
    """Remove all handlers and restore propagation on codeprobe logger."""
    import logging

    logger = logging.getLogger("codeprobe")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.propagate = True


def test_log_format_json_emits_valid_json():
    """--log-format=json produces parseable JSON log records."""
    import io
    import json
    import logging

    from codeprobe.cli import _configure_logging

    try:
        _configure_logging(verbose=0, quiet=False, log_format="json")
        logger = logging.getLogger("codeprobe")
        buf = io.StringIO()
        logger.handlers[0].stream = buf
        logger.info("test message")
        output = buf.getvalue().strip()
        assert output, "Expected JSON log output"
        json.loads(output)  # must not raise
    finally:
        _reset_codeprobe_logger()


def test_log_format_json_has_required_keys():
    """Each JSON log object has level, logger, message, timestamp."""
    import io
    import json
    import logging

    from codeprobe.cli import _configure_logging

    try:
        _configure_logging(verbose=0, quiet=False, log_format="json")
        logger = logging.getLogger("codeprobe")
        buf = io.StringIO()
        logger.handlers[0].stream = buf
        logger.info("test message")
        obj = json.loads(buf.getvalue().strip())
        for key in ("level", "logger", "message", "timestamp"):
            assert key in obj, f"Missing key '{key}' in {obj}"
    finally:
        _reset_codeprobe_logger()


def test_log_format_text_default_unchanged():
    """Default text format still works identically to bead 1."""
    import logging

    from codeprobe.cli import _JsonFormatter, _configure_logging

    try:
        _configure_logging(verbose=0, quiet=False, log_format="text")
        logger = logging.getLogger("codeprobe")
        assert logger.level == logging.INFO
        assert len(logger.handlers) == 1
        formatter = logger.handlers[0].formatter
        assert formatter is not None
        assert not isinstance(formatter, _JsonFormatter)
    finally:
        _reset_codeprobe_logger()


def test_log_format_option_appears_in_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "--log-format" in result.output
