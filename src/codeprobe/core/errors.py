"""Structured errors shared by the library and command-line interface."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CodeprobeError(Exception):
    """Base class for errors with stable machine-readable metadata."""

    code: str
    message: str
    terminal: bool = False
    message_for_agent: str | None = None
    detail: dict = field(default_factory=dict)
    exit_code: int = 2

    def __post_init__(self) -> None:
        super().__init__(self.message)


@dataclass
class PrescriptiveError(CodeprobeError):
    """Error carrying the exact flag and value needed for a safe retry."""

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
    """Terminal error carrying a diagnostic command and optional next steps."""

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
