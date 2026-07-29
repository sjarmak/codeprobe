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
from pathlib import Path
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
from codeprobe.cli.errors import PrescriptiveError

F = TypeVar("F", bound=Callable[..., Any])


def format_task_status(
    score: float,
    outcome: str,
) -> str:
    """Format a task result consistently across CLI output surfaces."""
    if outcome == "auth_failure":
        return "AUTH_ERROR"
    if outcome == "infra_failure":
        return "INFRA"
    if outcome == "error":
        return "ERROR"
    if score >= 1.0:
        return "PASS"
    if score <= 0.0:
        return "FAIL"
    return f"{score:.2f}"


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

    Side-effect: stashes the resolved mode on the current click context's
    ``obj`` dict under the key ``"codeprobe_output_mode"`` so the
    top-level :class:`~codeprobe.cli._error_handler.CodeprobeGroup` error
    renderer can honour the subcommand's flag choice even after the
    subcommand's own context has been torn down.
    """
    try:
        mode = resolve_output_mode(
            command=command,
            is_tty=sys.stdout.isatty(),
            explicit_json=json_flag,
            explicit_no_json=no_json_flag,
            explicit_json_lines=json_lines_flag,
            explicit_format=explicit_format,
            env=os.environ,
        )
    except ValueError as exc:
        # lint-exempt: mutually exclusive output flags are Click usage errors.
        raise click.UsageError(str(exc)) from exc

    # Propagate the resolved mode upward so error handlers can find it
    # without re-parsing flags.
    try:
        ctx = click.get_current_context(silent=True)
    except RuntimeError:
        ctx = None
    if ctx is not None:
        # Walk to the root context so siblings see the same mode. ctx.obj
        # is a shared dict when the root callback ran ctx.ensure_object.
        root = ctx
        while root.parent is not None:
            root = root.parent
        if isinstance(root.obj, dict):
            root.obj["codeprobe_output_mode"] = mode
            root.obj["codeprobe_command"] = command
    return mode


def resolve_explicit_mode(
    command: str,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> OutputMode:
    """Resolve the output mode for commands where JSON is opt-in.

    ``validate`` and the ``experiment`` subcommands predate the Big-5
    envelope contract and are scraped by existing scripts that expect
    pretty text even on non-TTY stdout. For them an explicit flag routes
    through :func:`resolve_mode` (keeping the mutex check and the
    error-handler stash); with no flag, pretty output wins regardless of
    TTY state. The no-flag path deliberately does NOT stash a mode, so
    typed errors raised later still render per the error handler's own
    TTY/env defaults (envelope on non-TTY stdout).
    """
    if json_flag or no_json_flag or json_lines_flag:
        return resolve_mode(command, json_flag, no_json_flag, json_lines_flag)
    return OutputMode(mode="pretty", use_rich=sys.stdout.isatty())


def validate_out_path(raw: str) -> Path:
    """Resolve and validate a user-supplied ``--out`` path.

    Shared by ``mine``, ``run``, and ``interpret`` — each treats ``--out`` as
    a destination it has not yet created (a directory for ``mine``/``run``, a
    report file for ``interpret``), so only the *parent* directory is
    required to already exist. Checking there, at the CLI boundary, turns a
    bad ``--out`` into one prescriptive error instead of a mid-run IOError
    from deep inside mining/execution/report-writing code.

    Raises :class:`PrescriptiveError` (``INVALID_OUT_PATH``) when the parent
    directory is missing or not writable.
    """
    path = Path(raw).expanduser().resolve()
    parent = path.parent
    if not parent.is_dir():
        raise PrescriptiveError(
            code="INVALID_OUT_PATH",
            message=(
                f"--out parent directory does not exist: {parent}. "
                "Create it first or choose a path under an existing directory."
            ),
            next_try_flag="--out",
            next_try_value=str(Path.cwd() / "out"),
            detail={"out": str(path), "parent": str(parent)},
        )
    if not os.access(parent, os.W_OK):
        raise PrescriptiveError(
            code="INVALID_OUT_PATH",
            message=f"--out parent directory is not writable: {parent}",
            next_try_flag="--out",
            next_try_value=str(Path.cwd() / "out"),
            detail={"out": str(path), "parent": str(parent)},
        )
    return path


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
    "resolve_explicit_mode",
    "resolve_mode",
    "validate_out_path",
]
