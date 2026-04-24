"""Machine-readable output envelope for the agent-friendly CLI.

Implements PRD §5.1 of ``docs/prd/prd_agent_friendly_cli.md`` — a terminating
single-line JSON document that every top-level CLI command emits when running
in ``--json`` mode.  The first field, ``record_type``, is a mandatory
discriminator so parsers can disambiguate terminating envelopes from NDJSON
event lines without asymmetric field checks.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from typing import IO, Any


def _resolve_codeprobe_version() -> str:
    """Return the installed codeprobe version, with defensive fallbacks."""
    try:  # pragma: no cover — happy path on any real install.
        from codeprobe import __version__ as pkg_version  # noqa: N813

        return pkg_version
    except Exception:  # pragma: no cover — defensive for odd dev checkouts.
        try:
            from importlib.metadata import version as _pkg_version

            return _pkg_version("codeprobe")
        except Exception:
            return "0.0.0"


CODEPROBE_VERSION: str = _resolve_codeprobe_version()

ENVELOPE_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class WarningEntry:
    """Non-terminal warning attached to an envelope."""

    code: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NextStep:
    """Suggested follow-up command the agent can execute verbatim."""

    summary: str
    command: str


@dataclass(frozen=True)
class ErrorPayload:
    """Structured error body carried on a failed envelope.

    ``kind`` distinguishes prescriptive errors (where ``next_try_flag`` /
    ``next_try_value`` tell the agent exactly what to retry) from diagnostic
    errors (where ``diagnose_cmd`` / ``message_for_agent`` describe what to
    investigate).  ``terminal`` marks errors the agent should not retry.
    """

    code: str
    message: str
    kind: str  # 'prescriptive' | 'diagnostic'
    terminal: bool = False
    next_try_flag: str | None = None
    next_try_value: str | None = None
    diagnose_cmd: str | None = None
    message_for_agent: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Envelope:
    """Terminating single-line JSON document for a CLI invocation.

    Field order is deliberate: ``record_type`` first so streaming parsers can
    disambiguate envelope-vs-event without inspecting multiple keys, followed
    by the summary fields, then the payload, then diagnostics.
    """

    record_type: str = field(default="envelope")
    ok: bool = True
    command: str = ""
    version: str = CODEPROBE_VERSION
    schema_version: str = ENVELOPE_SCHEMA_VERSION
    exit_code: int = 0
    data: dict[str, Any] | None = None
    error: ErrorPayload | None = None
    warnings: list[WarningEntry] = field(default_factory=list)
    next_steps: list[NextStep] = field(default_factory=list)


def emit(env: Envelope, stream: IO[str] | None = None) -> None:
    """Serialize *env* as a single JSON line and flush.

    Writes ``json.dumps(asdict(env), default=str) + '\\n'`` to *stream*
    (defaults to :data:`sys.stdout`) and calls ``flush``.  ``default=str``
    lets callers stash :class:`pathlib.Path` objects and other stringifiable
    values in ``data`` / ``detail`` without bespoke encoders.
    """
    out = stream if stream is not None else sys.stdout
    line = json.dumps(asdict(env), default=str)
    out.write(line + "\n")
    out.flush()
