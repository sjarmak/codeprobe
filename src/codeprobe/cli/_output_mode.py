"""Single source of truth for CLI output mode resolution.

Implements the TTY / environment / flag precedence rules defined in the PRD
sections 5.4, 5.5, and 13-T3. Every Big-5 command (``mine``, ``run``,
``interpret``, ``assess``, ``probe``) must call :func:`resolve_output_mode`
exactly once at startup to decide what to render — this keeps behaviour
consistent and makes the precedence testable in one place.

Key invariants (see PRD §13-T3, risk R7):

* An **explicit** CLI flag (``--json``, ``--no-json``, ``--json-lines``,
  ``--format``) **always** beats the ``CODEPROBE_JSON`` environment variable.
* ``CODEPROBE_JSON=1`` is *advisory*: it only nudges non-TTY sessions toward
  JSON output. It must **never** silently convert an interactive TTY session
  into JSON — that is the ``.bashrc``-corrupts-TTY bug the PRD calls out.
* The ``run`` command streams by default on non-TTY (NDJSON); all other
  Big-5 commands default to a single JSON envelope on non-TTY.

This module is deliberately prefixed with an underscore to signal that it is
an internal helper; callers outside ``codeprobe.cli`` should not import it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

OutputModeLiteral = Literal["pretty", "single_envelope", "ndjson"]

# Commands whose non-TTY default is a stream of NDJSON events rather than a
# single terminating envelope. Kept as a frozenset so the membership check is
# a cheap hash lookup and the set itself is immutable.
_NDJSON_DEFAULT_COMMANDS: frozenset[str] = frozenset({"run"})


@dataclass(frozen=True)
class OutputMode:
    """Resolved output mode for a CLI invocation.

    Attributes
    ----------
    mode:
        One of ``"pretty"``, ``"single_envelope"``, or ``"ndjson"``.
    use_rich:
        ``True`` iff the caller should use the Rich console renderer. Only
        true when the mode is ``"pretty"`` **and** stdout is a TTY — Rich
        rendering into a non-TTY produces ANSI garbage in log files.
    """

    mode: OutputModeLiteral
    use_rich: bool


def resolve_output_mode(
    command: str,
    is_tty: bool,
    explicit_json: bool,
    explicit_no_json: bool,
    explicit_json_lines: bool,
    explicit_format: str | None,
    env: Mapping[str, str],
) -> OutputMode:
    """Resolve the effective output mode from flags, TTY state, and env.

    Parameters
    ----------
    command:
        The Big-5 command name (``"mine"``, ``"run"``, ``"interpret"``,
        ``"assess"``, ``"probe"``). Controls the non-TTY default.
    is_tty:
        Whether stdout is attached to a TTY.
    explicit_json:
        ``True`` iff the user passed ``--json``.
    explicit_no_json:
        ``True`` iff the user passed ``--no-json``.
    explicit_json_lines:
        ``True`` iff the user passed ``--json-lines``.
    explicit_format:
        Value of ``--format`` if supplied, else ``None``. ``"text"`` forces
        pretty output; any other value is ignored by this resolver (format
        detail is the command's responsibility).
    env:
        Mapping of environment variables. Only ``CODEPROBE_JSON`` is read.

    Returns
    -------
    OutputMode
        The resolved mode and a ``use_rich`` hint.

    Raises
    ------
    ValueError
        If more than one explicit JSON-related flag is set. This is a
        defensive mutex check — it catches agent-orchestration bugs where a
        caller accidentally passes conflicting flags rather than silently
        preferring one. The click layer should also surface this via
        ``click.BadOptionUsage`` before we ever see it here, but we guard
        anyway.
    """
    # Mutex check: only one of the three JSON-mode booleans may be set.
    # ``--format`` is orthogonal and participates separately below.
    explicit_count = sum(
        1 for flag in (explicit_json, explicit_no_json, explicit_json_lines) if flag
    )
    if explicit_count > 1:
        raise ValueError(
            "Conflicting output flags: at most one of --json, --no-json, "
            "--json-lines may be specified."
        )

    # Explicit flags always win, regardless of TTY or env (PRD §13-T3, R7).
    if explicit_json_lines:
        return OutputMode(mode="ndjson", use_rich=False)
    if explicit_json:
        return OutputMode(mode="single_envelope", use_rich=False)
    if explicit_no_json:
        return OutputMode(mode="pretty", use_rich=is_tty)
    if explicit_format == "text":
        # ``--format=text`` is treated as an explicit request for pretty
        # output even in non-TTY contexts (e.g. redirecting to a file while
        # the user wants human-readable output).
        return OutputMode(mode="pretty", use_rich=is_tty)

    # No explicit flag — derive from TTY state.
    if not is_tty:
        # CODEPROBE_JSON is advisory on non-TTY, but note that the non-TTY
        # defaults already emit JSON, so the env var is effectively a no-op
        # here. We still read it to document the intent and to make future
        # changes (e.g. an env var that *forces* single-envelope even for
        # ``run``) explicit.
        _ = env.get("CODEPROBE_JSON")
        if command in _NDJSON_DEFAULT_COMMANDS:
            return OutputMode(mode="ndjson", use_rich=False)
        return OutputMode(mode="single_envelope", use_rich=False)

    # TTY path: CODEPROBE_JSON must NOT override — that is bug R7. Pretty
    # output wins unconditionally when stdout is interactive and no explicit
    # flag was supplied.
    return OutputMode(mode="pretty", use_rich=True)


__all__ = ["OutputMode", "resolve_output_mode"]
