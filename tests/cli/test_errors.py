"""Tests for codeprobe.cli.errors typed error classes.

Covers:
- Import surface (all three public classes importable).
- Successful construction of each class with required fields.
- Attribute defaults (exit_code, terminal, detail, message_for_agent, next_steps).
- Required-field enforcement: missing next_try_flag / next_try_value /
  diagnose_cmd raise TypeError.
- Exception inheritance: CodeprobeError derives from the builtin Exception,
  NOT from click.ClickException (PRD §6.1).
"""

from __future__ import annotations

import pytest

from codeprobe.cli.errors import (
    CodeprobeError,
    DiagnosticError,
    PrescriptiveError,
)


class TestCodeprobeError:
    def test_inherits_from_exception_not_click(self) -> None:
        err = CodeprobeError(code="X", message="boom")
        assert isinstance(err, Exception)

        # Must NOT inherit from click.ClickException (PRD §6.1).
        try:
            import click
        except ImportError:  # pragma: no cover - click is a project dep
            pytest.skip("click not installed")
        assert not isinstance(err, click.ClickException)

    def test_construction_basic(self) -> None:
        err = CodeprobeError(code="X", message="boom")
        assert err.code == "X"
        assert err.message == "boom"
        assert err.terminal is False
        assert err.message_for_agent is None
        assert err.detail == {}
        assert err.exit_code == 2

    def test_str_uses_message(self) -> None:
        err = CodeprobeError(code="X", message="boom")
        assert str(err) == "boom"

    def test_detail_defaults_are_independent(self) -> None:
        """field(default_factory=dict) must not share state between instances."""
        a = CodeprobeError(code="A", message="a")
        b = CodeprobeError(code="B", message="b")
        a.detail["k"] = 1
        assert b.detail == {}

    def test_can_be_raised_and_caught(self) -> None:
        with pytest.raises(CodeprobeError) as excinfo:
            raise CodeprobeError(code="X", message="boom")
        assert excinfo.value.code == "X"


class TestPrescriptiveError:
    def test_construction(self) -> None:
        err = PrescriptiveError(
            code="AMBIGUOUS_EXPERIMENT",
            message="Multiple experiments match",
            next_try_flag="--experiment",
            next_try_value="exp-123",
        )
        assert err.code == "AMBIGUOUS_EXPERIMENT"
        assert err.next_try_flag == "--experiment"
        assert err.next_try_value == "exp-123"

    def test_terminal_defaults_false(self) -> None:
        err = PrescriptiveError(
            code="X",
            message="m",
            next_try_flag="--foo",
            next_try_value="bar",
        )
        assert err.terminal is False

    def test_missing_next_try_flag_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            PrescriptiveError(  # type: ignore[call-arg]
                code="X",
                message="m",
                next_try_value="bar",
            )

    def test_missing_next_try_value_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            PrescriptiveError(  # type: ignore[call-arg]
                code="X",
                message="m",
                next_try_flag="--foo",
            )

    def test_missing_both_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            PrescriptiveError(code="X", message="m")  # type: ignore[call-arg]

    def test_is_codeprobe_error(self) -> None:
        err = PrescriptiveError(
            code="X", message="m", next_try_flag="--foo", next_try_value="bar"
        )
        assert isinstance(err, CodeprobeError)
        assert isinstance(err, Exception)


class TestDiagnosticError:
    def test_construction(self) -> None:
        err = DiagnosticError(
            code="SNAPSHOT_VERIFY_FAILED",
            message="snapshot corrupt",
            diagnose_cmd="codeprobe doctor",
        )
        assert err.code == "SNAPSHOT_VERIFY_FAILED"
        assert err.diagnose_cmd == "codeprobe doctor"

    def test_terminal_defaults_true(self) -> None:
        err = DiagnosticError(
            code="X", message="m", diagnose_cmd="codeprobe doctor"
        )
        assert err.terminal is True

    def test_next_steps_defaults_empty_list(self) -> None:
        err = DiagnosticError(
            code="X", message="m", diagnose_cmd="codeprobe doctor"
        )
        assert err.next_steps == []

    def test_next_steps_accepts_list_of_tuples(self) -> None:
        steps: list[tuple[str, str]] = [
            ("Run doctor", "codeprobe doctor"),
            ("Inspect logs", "tail -n 200 .codeprobe/logs/run.log"),
        ]
        err = DiagnosticError(
            code="X",
            message="m",
            diagnose_cmd="codeprobe doctor",
            next_steps=steps,
        )
        assert err.next_steps == steps
        assert err.next_steps[0] == ("Run doctor", "codeprobe doctor")

    def test_next_steps_default_is_independent(self) -> None:
        a = DiagnosticError(code="A", message="a", diagnose_cmd="cmd")
        b = DiagnosticError(code="B", message="b", diagnose_cmd="cmd")
        a.next_steps.append(("summary", "command"))
        assert b.next_steps == []

    def test_missing_diagnose_cmd_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            DiagnosticError(code="X", message="m")  # type: ignore[call-arg]

    def test_is_codeprobe_error(self) -> None:
        err = DiagnosticError(code="X", message="m", diagnose_cmd="cmd")
        assert isinstance(err, CodeprobeError)
        assert isinstance(err, Exception)
