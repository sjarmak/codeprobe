"""Backward-compatible CLI import surface for shared structured errors."""

from codeprobe.core.errors import (
    CodeprobeError,
    DiagnosticError,
    PrescriptiveError,
)

__all__ = ["CodeprobeError", "PrescriptiveError", "DiagnosticError"]
