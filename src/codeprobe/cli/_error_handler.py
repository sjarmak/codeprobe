"""Top-level click Group subclass that renders :class:`CodeprobeError`.

The handler is factored into its own module so that subgroups
(``snapshot``, ``check_infra``, ``experiment``, etc.) can share the same
:class:`CodeprobeGroup` base. Tests that invoke a subgroup directly (via
``CliRunner().invoke(check_infra, [...])``) therefore still route
``CodeprobeError`` through the envelope / pretty renderer instead of
letting the raw exception escape into the traceback.

See :mod:`codeprobe.cli.errors` for the typed-error hierarchy and
``docs/prd/prd_agent_friendly_cli.md`` §6 for the design rationale.
"""

from __future__ import annotations

import os
import sys

import click

from codeprobe.cli._output_helpers import emit_envelope
from codeprobe.cli._output_mode import OutputMode, resolve_output_mode
from codeprobe.cli.envelope import ErrorPayload, NextStep
from codeprobe.cli.errors import (
    CodeprobeError,
    DiagnosticError,
    PrescriptiveError,
)


def _error_payload(exc: CodeprobeError) -> ErrorPayload:
    """Convert a :class:`CodeprobeError` into the envelope's :class:`ErrorPayload`."""
    if isinstance(exc, PrescriptiveError):
        kind = "prescriptive"
        next_try_flag: str | None = exc.next_try_flag
        next_try_value: str | None = exc.next_try_value
        diagnose_cmd: str | None = None
    elif isinstance(exc, DiagnosticError):
        kind = "diagnostic"
        next_try_flag = None
        next_try_value = None
        diagnose_cmd = exc.diagnose_cmd
    else:  # pragma: no cover - base class not directly instantiated.
        kind = "diagnostic"
        next_try_flag = None
        next_try_value = None
        diagnose_cmd = None

    clean_detail = {
        k: v for k, v in exc.detail.items() if k != "_envelope_data"
    }
    return ErrorPayload(
        code=exc.code,
        message=exc.message,
        kind=kind,
        terminal=exc.terminal,
        next_try_flag=next_try_flag,
        next_try_value=next_try_value,
        diagnose_cmd=diagnose_cmd,
        message_for_agent=exc.message_for_agent,
        detail=clean_detail,
    )


def _deepest_info_name(ctx: click.Context | None) -> str:
    """Return the most-specific command name in a click context chain.

    Prefers the subcommand that was actually invoked (via
    ``ctx.invoked_subcommand``) when available so nested groups like
    ``codeprobe snapshot create`` surface as ``snapshot create`` in the
    emitted envelope rather than as the root group name.
    """
    if ctx is None:
        return "codeprobe"

    # Walk up to the root to gather the invocation chain.
    chain: list[click.Context] = []
    sub_ctx: click.Context | None = ctx
    while sub_ctx is not None:
        chain.append(sub_ctx)
        sub_ctx = sub_ctx.parent
    chain.reverse()  # root → leaf

    # Skip the root context's info_name — it's the click group's Python
    # name ("main" / "codeprobe") and isn't meaningful to agent consumers.
    # Use the invoked-subcommand chain instead.
    if len(chain) == 1:
        # Only a root context is present — e.g. the error came from the
        # root callback itself. Surface invoked_subcommand when possible.
        only = chain[0]
        if only.invoked_subcommand:
            return only.invoked_subcommand
        return "codeprobe"

    subcommand_contexts = chain[1:]
    names: list[str] = [c.info_name for c in subcommand_contexts if c.info_name]

    # If the invoked subcommand of the deepest ctx is a further nested
    # group (e.g. ``check-infra drift``), stitch it on.
    leaf = chain[-1]
    if leaf.invoked_subcommand and leaf.invoked_subcommand not in names:
        names.append(leaf.invoked_subcommand)

    if names:
        return " ".join(names)

    return "codeprobe"


def _resolve_error_output_mode(ctx: click.Context | None) -> OutputMode:
    """Resolve the output mode at error time.

    Precedence (highest wins):

    1. ``ctx.obj["codeprobe_output_mode"]`` — set by
       :func:`codeprobe.cli._output_helpers.resolve_mode` the moment the
       subcommand resolved its flags. This is the authoritative value
       because it captures the exact ``--json`` / ``--no-json`` /
       ``--json-lines`` intent the user passed.
    2. Context param walk — picks up flags set on any currently-live
       context, as a fallback for callers that raise before resolving
       their output mode (e.g. during Click option parsing).
    3. TTY / env defaults via :func:`resolve_output_mode`.
    """
    command_name = _deepest_info_name(ctx)

    # Step 1: honour a previously-resolved mode if the subcommand stashed one.
    if ctx is not None:
        root = ctx
        while root.parent is not None:
            root = root.parent
        if isinstance(root.obj, dict):
            cached = root.obj.get("codeprobe_output_mode")
            if isinstance(cached, OutputMode):
                return cached

    # Step 2: walk params on whatever contexts remain.
    explicit_json = False
    explicit_no_json = False
    explicit_json_lines = False
    sub_ctx = ctx
    while sub_ctx is not None:
        params = sub_ctx.params or {}
        if params.get("json_flag"):
            explicit_json = True
        if params.get("no_json_flag"):
            explicit_no_json = True
        if params.get("json_lines_flag"):
            explicit_json_lines = True
        sub_ctx = sub_ctx.parent

    explicit_count = sum(
        1 for f in (explicit_json, explicit_no_json, explicit_json_lines) if f
    )
    if explicit_count > 1:
        if explicit_json_lines:
            explicit_json = False
            explicit_no_json = False
        elif explicit_json:
            explicit_no_json = False

    return resolve_output_mode(
        command=command_name,
        is_tty=sys.stdout.isatty(),
        explicit_json=explicit_json,
        explicit_no_json=explicit_no_json,
        explicit_json_lines=explicit_json_lines,
        explicit_format=None,
        env=os.environ,
    )


def render_codeprobe_error(
    ctx: click.Context | None, exc: CodeprobeError
) -> None:
    """Render a :class:`CodeprobeError` per the resolved output mode."""
    mode = _resolve_error_output_mode(ctx)
    command_name = _deepest_info_name(ctx)

    if mode.mode in ("single_envelope", "ndjson"):
        next_steps_payload: list[NextStep] = []
        if isinstance(exc, DiagnosticError):
            for summary, command in exc.next_steps:
                next_steps_payload.append(
                    NextStep(summary=summary, command=command)
                )
        # Allow callers to pipe additional command-level state into the
        # envelope's ``data`` block by stashing it under the reserved
        # ``_envelope_data`` key of ``detail``. The key is stripped from
        # the serialised detail so it never leaks into the error body.
        envelope_data: dict | None = None
        if isinstance(exc.detail, dict):
            raw = exc.detail.get("_envelope_data")
            if isinstance(raw, dict):
                envelope_data = dict(raw)
        emit_envelope(
            command=command_name,
            ok=False,
            data=envelope_data if envelope_data is not None else {},
            error=_error_payload(exc),
            exit_code=exc.exit_code,
            next_steps=next_steps_payload,
        )
        return

    banner = f"ERROR [{exc.code}]"
    if exc.terminal:
        banner += " (terminal)"
    click.echo(banner, err=True)
    click.echo(exc.message, err=True)
    if isinstance(exc, PrescriptiveError):
        suggestion = exc.next_try_flag
        if exc.next_try_value:
            suggestion = f"{suggestion} {exc.next_try_value}".rstrip()
        click.echo(f"  Retry with: {suggestion}", err=True)
    elif isinstance(exc, DiagnosticError):
        click.echo(f"  Diagnose: {exc.diagnose_cmd}", err=True)
        for summary, command in exc.next_steps:
            click.echo(f"    - {summary}: {command}", err=True)


class CodeprobeGroup(click.Group):
    """click.Group that catches :class:`CodeprobeError` and renders it.

    Subclasses inherit the handler. Subgroups (``snapshot``, ``check-infra``,
    ``experiment``) use this class too so tests that invoke the subgroup
    directly via :class:`click.testing.CliRunner` still exercise the
    typed-error rendering path rather than surfacing a raw traceback.
    """

    def invoke(self, ctx: click.Context) -> object:
        try:
            return super().invoke(ctx)
        except CodeprobeError as exc:
            render_codeprobe_error(ctx, exc)
            ctx.exit(exc.exit_code)


__all__ = ["CodeprobeGroup", "render_codeprobe_error"]
