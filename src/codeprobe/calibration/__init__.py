"""Calibration — inter-curator agreement gate and profile emission.

R11 validity gate: a calibration profile may only be emitted when two
independent curators agree on a holdout set at Pearson correlation >=
threshold (default 0.6) AND the holdout has >=100 tasks drawn from >=3
repositories.

This module ships the code path and schema only. The partner-gated holdout
data (>=100 hand-annotated tasks from >=3 non-OSS repos) is out of scope
for this unit — see ``docs/CALIBRATION.md``.
"""

from __future__ import annotations

from codeprobe.calibration.gate import (
    CalibrationRejectedError,
    HoldoutRow,
    compute_pearson,
    emit_profile,
    format_calibration_line,
    load_holdout,
    refuse_profile_emission,
    validate_calibration_correlation,
)
from codeprobe.calibration.profile import CalibrationProfile


def __getattr__(name: str) -> object:
    """Re-export shim for the ``CalibrationRejected`` → ``CalibrationRejectedError``
    rename (N818). See :mod:`codeprobe.calibration.gate` for the full alias table.
    """
    if name == "CalibrationRejected":
        from codeprobe.calibration.gate import __getattr__ as _gate_getattr

        return _gate_getattr(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "CalibrationProfile",
    "CalibrationRejectedError",
    "HoldoutRow",
    "compute_pearson",
    "emit_profile",
    "format_calibration_line",
    "load_holdout",
    "refuse_profile_emission",
    "validate_calibration_correlation",
]
