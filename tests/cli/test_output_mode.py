"""Tests for ``codeprobe.cli._output_mode.resolve_output_mode``.

The acceptance table in PRD §5.5 / §13-T3 is the spec. Each row is encoded
as a ``pytest.mark.parametrize`` case below. If a new case is added to the
PRD, add a row here and update the resolver to match — the table is the
contract.
"""

from __future__ import annotations

import pytest

from codeprobe.cli._output_mode import OutputMode, resolve_output_mode


def _call(
    *,
    command: str = "mine",
    is_tty: bool = False,
    explicit_json: bool = False,
    explicit_no_json: bool = False,
    explicit_json_lines: bool = False,
    explicit_format: str | None = None,
    env: dict[str, str] | None = None,
) -> OutputMode:
    """Thin keyword-only wrapper so each test reads like the acceptance row."""
    return resolve_output_mode(
        command=command,
        is_tty=is_tty,
        explicit_json=explicit_json,
        explicit_no_json=explicit_no_json,
        explicit_json_lines=explicit_json_lines,
        explicit_format=explicit_format,
        env=env if env is not None else {},
    )


def test_output_mode_is_frozen_dataclass() -> None:
    """``OutputMode`` must be frozen so callers cannot mutate resolved state."""
    mode = OutputMode(mode="pretty", use_rich=True)
    with pytest.raises(Exception):  # FrozenInstanceError subclasses AttributeError
        mode.mode = "ndjson"  # type: ignore[misc]


@pytest.mark.parametrize(
    "case_id,kwargs,expected",
    [
        # AC4: mine + TTY + no flags → pretty
        (
            "mine_tty_defaults",
            {"command": "mine", "is_tty": True},
            OutputMode(mode="pretty", use_rich=True),
        ),
        # AC5: mine + non-TTY + no flags → single_envelope
        (
            "mine_non_tty_defaults",
            {"command": "mine", "is_tty": False},
            OutputMode(mode="single_envelope", use_rich=False),
        ),
        # AC6: run + non-TTY + no flags → ndjson (§5.4/T6)
        (
            "run_non_tty_defaults_to_ndjson",
            {"command": "run", "is_tty": False},
            OutputMode(mode="ndjson", use_rich=False),
        ),
        # AC7: run + TTY + no flags → pretty
        (
            "run_tty_defaults",
            {"command": "run", "is_tty": True},
            OutputMode(mode="pretty", use_rich=True),
        ),
        # AC8: run + non-TTY + --json → single_envelope (--json collapses stream)
        (
            "run_non_tty_explicit_json_collapses_stream",
            {"command": "run", "is_tty": False, "explicit_json": True},
            OutputMode(mode="single_envelope", use_rich=False),
        ),
        # AC9: mine + non-TTY + --json-lines → ndjson
        (
            "mine_non_tty_explicit_json_lines",
            {"command": "mine", "is_tty": False, "explicit_json_lines": True},
            OutputMode(mode="ndjson", use_rich=False),
        ),
        # AC10: --no-json beats env (R7)
        (
            "no_json_flag_beats_env",
            {
                "command": "mine",
                "is_tty": True,
                "explicit_no_json": True,
                "env": {"CODEPROBE_JSON": "1"},
            },
            OutputMode(mode="pretty", use_rich=True),
        ),
        # AC11: env does NOT override TTY with no explicit flag (R7)
        (
            "env_does_not_override_tty",
            {"command": "mine", "is_tty": True, "env": {"CODEPROBE_JSON": "1"}},
            OutputMode(mode="pretty", use_rich=True),
        ),
        # AC12: mine + non-TTY + CODEPROBE_JSON=1 → single_envelope
        (
            "mine_non_tty_with_env",
            {"command": "mine", "is_tty": False, "env": {"CODEPROBE_JSON": "1"}},
            OutputMode(mode="single_envelope", use_rich=False),
        ),
        # AC13: run + non-TTY + CODEPROBE_JSON=1 → ndjson (streaming default preserved)
        (
            "run_non_tty_with_env_keeps_ndjson",
            {"command": "run", "is_tty": False, "env": {"CODEPROBE_JSON": "1"}},
            OutputMode(mode="ndjson", use_rich=False),
        ),
        # AC14: mine + TTY + --json → single_envelope (explicit beats TTY)
        (
            "mine_tty_explicit_json",
            {"command": "mine", "is_tty": True, "explicit_json": True},
            OutputMode(mode="single_envelope", use_rich=False),
        ),
        # AC15: mine + TTY + --json-lines → ndjson (explicit beats TTY)
        (
            "mine_tty_explicit_json_lines",
            {"command": "mine", "is_tty": True, "explicit_json_lines": True},
            OutputMode(mode="ndjson", use_rich=False),
        ),
        # AC16: --format=text beats env CODEPROBE_JSON=1 even on non-TTY
        (
            "format_text_beats_env",
            {
                "command": "interpret",
                "is_tty": False,
                "explicit_format": "text",
                "env": {"CODEPROBE_JSON": "1"},
            },
            OutputMode(mode="pretty", use_rich=False),
        ),
    ],
)
def test_resolve_output_mode_table(
    case_id: str, kwargs: dict[str, object], expected: OutputMode
) -> None:
    """Every row in the PRD acceptance table must resolve exactly."""
    result = _call(**kwargs)  # type: ignore[arg-type]
    assert result == expected, f"case {case_id}: expected {expected}, got {result}"


def test_mutex_json_and_no_json_raises() -> None:
    """AC17: passing both --json and --no-json must raise ValueError."""
    with pytest.raises(ValueError, match="Conflicting output flags"):
        _call(
            command="mine",
            is_tty=True,
            explicit_json=True,
            explicit_no_json=True,
        )


def test_mutex_json_and_json_lines_raises() -> None:
    """Defensive: --json and --json-lines together is also a mutex violation."""
    with pytest.raises(ValueError, match="Conflicting output flags"):
        _call(explicit_json=True, explicit_json_lines=True)


def test_mutex_no_json_and_json_lines_raises() -> None:
    """Defensive: --no-json and --json-lines together is also a mutex violation."""
    with pytest.raises(ValueError, match="Conflicting output flags"):
        _call(explicit_no_json=True, explicit_json_lines=True)


def test_mutex_all_three_raises() -> None:
    """Defensive: all three JSON flags together is a mutex violation."""
    with pytest.raises(ValueError, match="Conflicting output flags"):
        _call(
            explicit_json=True,
            explicit_no_json=True,
            explicit_json_lines=True,
        )


def test_no_json_on_non_tty_disables_rich() -> None:
    """``--no-json`` on a non-TTY yields pretty mode but ``use_rich=False``.

    Rich renders ANSI escape codes; emitting them into a pipe or file
    produces unreadable output, so ``use_rich`` tracks the TTY state
    independently of the mode. Not in the explicit acceptance table but
    implied by the ``use_rich = (mode == 'pretty' and is_tty)`` rule.
    """
    result = _call(command="mine", is_tty=False, explicit_no_json=True)
    assert result.mode == "pretty"
    assert result.use_rich is False
