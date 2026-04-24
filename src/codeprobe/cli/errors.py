"""Typed error classes for codeprobe CLI.

This module defines the structured error hierarchy used by CLI commands to
communicate failures to both humans and agent callers.  The design is
deliberately *not* based on ``click.ClickException``:

- Click's ``ClickException`` uses mutable class-level attributes (``exit_code``
  is a class attribute) which fights against the frozen/declarative
  ``@dataclass`` pattern we want for predictable, agent-friendly errors.
- We want these errors to be serializable to JSON (for ``--json`` output and
  the agent envelope) without inheriting Click's terminal-rendering assumptions.
- Click's rendering of ``ClickException`` goes straight to stderr as free-form
  text; we instead route through the JSON envelope writer.

See the Agent-Friendly CLI PRD §6 for the full rationale.

Public API
----------

``CodeprobeError``
    Base for all codeprobe-specific errors.  Carries a stable ``code``
    identifier, human message, an optional agent-oriented message, a
    ``detail`` dict for structured context, a ``terminal`` flag (whether the
    error is intrinsically fatal), and an ``exit_code``.

``PrescriptiveError``
    An error that tells the caller *exactly* what to do next.  Requires a
    ``next_try_flag`` and ``next_try_value`` so an agent can mechanically
    retry with the corrected invocation.  Defaults to non-terminal.

``DiagnosticError``
    An error where no single next-flag fix is safe or correct.  Requires a
    ``diagnose_cmd`` (e.g., ``codeprobe doctor``) and may carry an ordered
    list of ``next_steps`` (summary, command) tuples.  Defaults to terminal.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CodeprobeError(Exception):
    """Base class for structured codeprobe CLI errors.

    Attributes
    ----------
    code:
        Stable, UPPER_SNAKE_CASE identifier (e.g., ``NO_EXPERIMENT``).  Used
        by agents and scripts to branch on error kind.
    message:
        Human-readable message; rendered in the terminal or dashboard.
    terminal:
        ``True`` when the error is intrinsically fatal and should not be
        retried without user intervention.  Subclasses override the default.
    message_for_agent:
        Optional alternate phrasing tuned for agent consumers (more
        imperative, fewer pleasantries).  ``None`` falls back to ``message``.
    detail:
        Free-form structured context (paths, counts, config snapshots).
        Must be JSON-serializable.
    exit_code:
        Process exit code.  Defaults to ``2`` (CLI misuse / recoverable
        failure) to distinguish from ``1`` (generic error) and ``0``
        (success).
    """

    code: str
    message: str
    terminal: bool = False
    message_for_agent: str | None = None
    detail: dict = field(default_factory=dict)
    exit_code: int = 2

    def __post_init__(self) -> None:
        # Initialize the underlying Exception with the human message so
        # ``str(err)`` / ``repr(err)`` produce sensible output even when the
        # error is logged via a plain traceback.
        super().__init__(self.message)


@dataclass
class PrescriptiveError(CodeprobeError):
    """Error that names the exact flag+value to retry with.

    Use this when the fix is a single deterministic flag change — for
    example, resolving an ambiguity by passing ``--experiment <id>``.  Agents
    can retry mechanically without further reasoning.

    ``next_try_flag`` and ``next_try_value`` are required (declared before
    any defaulted attributes so dataclass construction raises ``TypeError``
    when they are omitted).
    """

    # Non-default fields must come before any inherited defaulted fields
    # are overridden.  We re-declare inherited fields below to preserve
    # argument order per PEP 557 rules.
    next_try_flag: str = field(default=None)  # type: ignore[assignment]
    next_try_value: str = field(default=None)  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.next_try_flag is None:
            raise TypeError(
                "PrescriptiveError requires 'next_try_flag' "
                "(the exact CLI flag to retry with, e.g. '--experiment')"
            )
        if self.next_try_value is None:
            raise TypeError(
                "PrescriptiveError requires 'next_try_value' "
                "(the value to pass to next_try_flag)"
            )
        super().__post_init__()


@dataclass
class DiagnosticError(CodeprobeError):
    """Error that cannot be resolved by a single flag change.

    Use this when the right response is diagnostic — running ``codeprobe
    doctor``, inspecting snapshot integrity, reviewing a canary gate
    report, or running arbitrary shell commands to inspect state.  Agents
    are expected to treat these as terminal by default and escalate.

    ``diagnose_cmd`` is required; ``next_steps`` is an ordered list of
    ``(summary, command)`` pairs the caller can present or execute.
    ``terminal`` defaults to ``True``.
    """

    diagnose_cmd: str = field(default=None)  # type: ignore[assignment]
    next_steps: list[tuple[str, str]] = field(default_factory=list)
    terminal: bool = True

    def __post_init__(self) -> None:
        if self.diagnose_cmd is None:
            raise TypeError(
                "DiagnosticError requires 'diagnose_cmd' "
                "(the command users should run to investigate, "
                "e.g. 'codeprobe doctor')"
            )
        super().__post_init__()


__all__ = ["CodeprobeError", "PrescriptiveError", "DiagnosticError"]
