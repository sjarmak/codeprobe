"""Regression tests for ``run_eval``'s trace_overflow argument validation.

Previously the library-level guard used ``SystemExit(2)`` for an invalid
``trace_overflow`` value, which leaked the CLI exit-code contract onto
programmatic callers. The v0.6.0-batch-a review (simplification #10)
asked for a proper ``ValueError`` at the library boundary; the CLI layer
already constrains the surface via ``click.Choice``.
"""

from __future__ import annotations

import pytest


def test_run_eval_invalid_trace_overflow_raises_value_error() -> None:
    """Programmatic callers get a ValueError, not SystemExit."""
    from codeprobe.cli import run_cmd as run_cmd_mod

    with pytest.raises(ValueError, match="trace_overflow"):
        run_cmd_mod.run_eval(
            ".",
            trace_overflow="bogus",
        )


def test_run_eval_invalid_trace_overflow_does_not_raise_system_exit() -> None:
    """SystemExit should not be used for library-level validation."""
    from codeprobe.cli import run_cmd as run_cmd_mod

    # SystemExit is a BaseException subclass, not Exception — be explicit.
    try:
        run_cmd_mod.run_eval(
            ".",
            trace_overflow="invalid",
        )
    except ValueError:
        pass  # Expected.
    except SystemExit as exc:
        pytest.fail(
            f"run_eval raised SystemExit for invalid trace_overflow: {exc}"
        )
