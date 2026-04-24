"""CalibrationProfile — immutable artifact recording inter-curator agreement.

Emitted only when the R11 calibration gate passes. Consumers read this to
display ``calibration_confidence`` alongside assessment output so users can
gauge how much to trust curator-derived scores.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class CalibrationProfile:
    """Inter-curator calibration profile.

    Attributes:
        correlation_coefficient: Pearson correlation between two curators on
            the holdout set. Range [-1.0, 1.0]. Values >= 0.6 pass the gate.
        holdout_size: Number of tasks in the holdout set. Must be >= 100 for
            the gate to pass.
        holdout_repos: Tuple of repo names/IDs contributing holdout tasks.
            Must contain >= 3 distinct repos for the gate to pass.
        produced_at: ISO 8601 UTC timestamp of profile emission.
        curator_version: Version identifier for the curator model/harness
            used to score the holdout.
    """

    correlation_coefficient: float
    holdout_size: int
    holdout_repos: tuple[str, ...]
    produced_at: str
    curator_version: str

    @staticmethod
    def utcnow_iso() -> str:
        """Return current UTC time as ISO 8601 (seconds precision)."""
        return datetime.now(UTC).replace(microsecond=0).isoformat()

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dict view of this profile.

        Includes a ``calibration_confidence`` alias keyed to the correlation
        coefficient. This is the canonical field that downstream surfaces
        (``codeprobe assess``, ``codeprobe calibrate``) display to users.
        """
        return {
            "correlation_coefficient": self.correlation_coefficient,
            "calibration_confidence": self.correlation_coefficient,
            "holdout_size": self.holdout_size,
            "holdout_repos": list(self.holdout_repos),
            "produced_at": self.produced_at,
            "curator_version": self.curator_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> CalibrationProfile:
        """Validate-or-die parsing of a profile dict.

        Raises:
            ValueError: if any required field is missing or ill-typed.
        """
        required = (
            "correlation_coefficient",
            "holdout_size",
            "holdout_repos",
            "produced_at",
            "curator_version",
        )
        missing = [k for k in required if k not in payload]
        if missing:
            raise ValueError(
                f"CalibrationProfile payload missing fields: {missing}"
            )

        correlation = payload["correlation_coefficient"]
        if not isinstance(correlation, (int, float)):
            raise ValueError("correlation_coefficient must be numeric")

        holdout_size = payload["holdout_size"]
        if not isinstance(holdout_size, int) or isinstance(holdout_size, bool):
            raise ValueError("holdout_size must be int")

        holdout_repos_raw = payload["holdout_repos"]
        if not isinstance(holdout_repos_raw, (list, tuple)):
            raise ValueError("holdout_repos must be a list/tuple")
        holdout_repos = tuple(str(r) for r in holdout_repos_raw)

        produced_at = payload["produced_at"]
        if not isinstance(produced_at, str):
            raise ValueError("produced_at must be str")

        curator_version = payload["curator_version"]
        if not isinstance(curator_version, str):
            raise ValueError("curator_version must be str")

        return cls(
            correlation_coefficient=float(correlation),
            holdout_size=holdout_size,
            holdout_repos=holdout_repos,
            produced_at=produced_at,
            curator_version=curator_version,
        )
