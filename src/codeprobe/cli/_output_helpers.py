"""Helpers for wiring ``--json / --no-json / --json-lines`` flags and
emitting envelopes/NDJSON events from CLI commands.

This module sits beside :mod:`codeprobe.cli._output_mode` and
:mod:`codeprobe.cli.envelope` and is the single integration point for the
Big-5 + diagnostic commands. The helpers are deliberately small and
mechanical — ZFC compliant — they only build structured records and
delegate serialization to :func:`codeprobe.cli.envelope.emit`.

Usage::

    @main.command()
    @add_json_flags
    @click.pass_context
    def doctor(ctx, json_flag, no_json_flag, json_lines_flag):
        mode = resolve_mode("doctor", json_flag, no_json_flag,
                            json_lines_flag)
        # ... run command logic ...
        if mode.mode in ("single_envelope", "ndjson"):
            emit_envelope(command="doctor", data={"command_schema_version": "1"})
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Iterable
from typing import Any, TypeVar

import click

from codeprobe.cli._output_mode import OutputMode, resolve_output_mode
from codeprobe.cli.envelope import (
    Envelope,
    ErrorPayload,
    NextStep,
    WarningEntry,
    emit,
)

F = TypeVar("F", bound=Callable[..., Any])


def add_json_flags(command: F) -> F:
    """Decorate a click command with ``--json / --no-json / --json-lines``.

    The flags are named ``json_flag``, ``no_json_flag``, and
    ``json_lines_flag`` on the wrapped function so they don't collide with
    Python builtins or the command's own kwargs. Click stacks decorators
    bottom-up, so the order here matches the surface order expected in
    ``--help`` output (``--json``, ``--no-json``, ``--json-lines``).
    """
    command = click.option(
        "--json-lines",
        "json_lines_flag",
        is_flag=True,
        default=False,
        help="Emit NDJSON (per-record JSON lines) to stdout.",
    )(command)
    command = click.option(
        "--no-json",
        "no_json_flag",
        is_flag=True,
        default=False,
        help="Force pretty output (overrides CODEPROBE_JSON env).",
    )(command)
    command = click.option(
        "--json",
        "json_flag",
        is_flag=True,
        default=False,
        help="Emit single-envelope JSON to stdout.",
    )(command)
    return command


def resolve_mode(
    command: str,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
    explicit_format: str | None = None,
) -> OutputMode:
    """Resolve the effective :class:`OutputMode` for a CLI invocation.

    Catches the mutex :class:`ValueError` from
    :func:`resolve_output_mode` and re-raises it as a
    :class:`click.UsageError` so Click surfaces the expected usage-error
    exit code / formatting instead of a bare traceback.
    """
    try:
        return resolve_output_mode(
            command=command,
            is_tty=sys.stdout.isatty(),
            explicit_json=json_flag,
            explicit_no_json=no_json_flag,
            explicit_json_lines=json_lines_flag,
            explicit_format=explicit_format,
            env=os.environ,
        )
    except ValueError as exc:
        raise click.UsageError(str(exc)) from exc


def _normalise_data(data: dict[str, Any] | None) -> dict[str, Any]:
    """Ensure the envelope ``data`` payload carries ``command_schema_version``.

    The agent-friendly CLI contract requires every envelope to advertise
    ``data.command_schema_version`` so consumers can detect the contract
    they are parsing. Callers can still override by passing the key
    themselves — this helper only fills in the default.
    """
    payload: dict[str, Any] = dict(data) if data is not None else {}
    payload.setdefault("command_schema_version", "1")
    return payload


def emit_envelope(
    *,
    command: str,
    data: dict[str, Any] | None = None,
    ok: bool = True,
    warnings: Iterable[WarningEntry] | None = None,
    next_steps: Iterable[NextStep] | None = None,
    error: ErrorPayload | None = None,
    exit_code: int = 0,
) -> None:
    """Build and emit an :class:`Envelope` to stdout.

    ``data`` is normalised so ``command_schema_version`` is always present.
    The envelope ``record_type`` defaults to ``"envelope"`` (set in the
    dataclass itself). All other fields mirror the dataclass defaults.
    """
    env = Envelope(
        ok=ok,
        command=command,
        exit_code=exit_code,
        data=_normalise_data(data),
        error=error,
        warnings=list(warnings) if warnings is not None else [],
        next_steps=list(next_steps) if next_steps is not None else [],
    )
    emit(env)


def emit_event(record: dict[str, Any]) -> None:
    """Emit a single NDJSON ``event`` record on stdout.

    ``record_type`` is forced to ``"event"`` regardless of what the caller
    passes — this preserves the discriminator contract from PRD §5.1.
    """
    payload = dict(record)
    payload["record_type"] = "event"
    line = json.dumps(payload, default=str)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


__all__ = [
    "add_json_flags",
    "emit_envelope",
    "emit_event",
    "resolve_mode",
]
