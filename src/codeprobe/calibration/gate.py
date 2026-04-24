"""Calibration gate — enforce R11 validity requirements before emitting a
``CalibrationProfile``.

Gate policy:

* Pearson correlation between the two curators on the holdout set must be
  at or above ``threshold`` (default 0.6).
* Holdout must contain at least ``min_tasks`` rows (default 100).
* Holdout must span at least ``min_repos`` distinct repositories
  (default 3).

Any violation raises :class:`CalibrationRejectedError`.

Why this lives in code (not in policy docs alone)
-------------------------------------------------

The gate is deterministic arithmetic with explicit thresholds. Per the
project's ZFC rules, this falls under the "deterministic ranking with
explicit tiebreaker rules" justified exception: there is no semantic
judgment, only a numeric comparison against a named threshold.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from codeprobe.calibration.profile import CalibrationProfile


class CalibrationRejectedError(Exception):
    """Raised when a calibration profile fails the R11 validity gate."""


_LEGACY_EXCEPTION_ALIASES = {
    "CalibrationRejected": "CalibrationRejectedError",
}


def __getattr__(name: str) -> object:
    """Legacy-alias shim — ``CalibrationRejected`` → ``CalibrationRejectedError``.

    PEP 8 / ruff N818 require an ``Error`` suffix. The alias keeps
    external callers working across one minor-version cycle and emits
    :class:`DeprecationWarning` on access so they know to migrate.
    Removal is planned for v0.9.
    """
    new_name = _LEGACY_EXCEPTION_ALIASES.get(name)
    if new_name is not None:
        import warnings

        warnings.warn(
            f"{name} is deprecated; use {new_name}. "
            "The alias will be removed in v0.9.",
            DeprecationWarning,
            stacklevel=2,
        )
        return globals()[new_name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass(frozen=True)
class HoldoutRow:
    """One row of a holdout dataset: two curators scoring the same task.

    Attributes:
        task_id: Stable identifier for the task.
        curator_a: First curator's score (float in [0, 1] by convention).
        curator_b: Second curator's score (float in [0, 1] by convention).
        repo: Repository identifier this task belongs to.
    """

    task_id: str
    curator_a: float
    curator_b: float
    repo: str


def load_holdout(path: Path) -> tuple[HoldoutRow, ...]:
    """Validate-or-die loader for holdout JSON.

    Expects a JSON array of objects with keys ``task_id``, ``curator_a``,
    ``curator_b``, ``repo``.

    Raises:
        CalibrationRejectedError: if the file is missing, malformed, or any row
            is ill-typed.
    """
    if not path.exists():
        raise CalibrationRejectedError(f"Holdout file does not exist: {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CalibrationRejectedError(f"Holdout JSON is invalid: {exc}") from exc

    if not isinstance(raw, list):
        raise CalibrationRejectedError("Holdout JSON must be a list of rows")

    rows: list[HoldoutRow] = []
    required = ("task_id", "curator_a", "curator_b", "repo")
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            raise CalibrationRejectedError(f"Holdout row {i} is not an object")
        missing = [k for k in required if k not in row]
        if missing:
            raise CalibrationRejectedError(
                f"Holdout row {i} missing fields: {missing}"
            )
        try:
            rows.append(
                HoldoutRow(
                    task_id=str(row["task_id"]),
                    curator_a=float(row["curator_a"]),
                    curator_b=float(row["curator_b"]),
                    repo=str(row["repo"]),
                )
            )
        except (TypeError, ValueError) as exc:
            raise CalibrationRejectedError(
                f"Holdout row {i} has bad types: {exc}"
            ) from exc

    return tuple(rows)


def compute_pearson(x: Sequence[float], y: Sequence[float]) -> float:
    """Pearson correlation coefficient over ``x`` and ``y``.

    Thin wrapper over :func:`statistics.correlation` with defensive checks
    and a typed error. Deterministic: identical inputs always yield the
    identical float.

    Raises:
        CalibrationRejectedError: if lengths differ, size < 2, or either series
            has zero variance (undefined correlation).
    """
    if len(x) != len(y):
        raise CalibrationRejectedError(
            f"Pearson requires equal-length sequences (got {len(x)} vs {len(y)})"
        )
    if len(x) < 2:
        raise CalibrationRejectedError(
            "Pearson requires at least 2 observations per sequence"
        )
    # statistics.correlation raises StatisticsError on zero variance; catch
    # and re-raise as our typed error so callers have a single exception to
    # trap.
    try:
        return statistics.correlation(x, y)
    except statistics.StatisticsError as exc:
        raise CalibrationRejectedError(
            f"Pearson correlation undefined (zero variance): {exc}"
        ) from exc


def refuse_profile_emission(
    holdout: Iterable[HoldoutRow],
    *,
    min_tasks: int = 100,
    min_repos: int = 3,
) -> None:
    """Raise :class:`CalibrationRejectedError` if the holdout is too small/thin.

    The partner-gated R11 requirement is >=100 tasks across >=3 non-OSS
    repositories. This function enforces the numeric portion; partner
    provenance (non-OSS attestation) is out of scope for this code path.
    """
    rows = tuple(holdout)
    n = len(rows)
    if n < min_tasks:
        raise CalibrationRejectedError(
            f"Holdout too small: {n} tasks (need >= {min_tasks})"
        )
    distinct_repos = {row.repo for row in rows}
    if len(distinct_repos) < min_repos:
        raise CalibrationRejectedError(
            f"Holdout spans too few repos: {len(distinct_repos)} "
            f"(need >= {min_repos})"
        )


def validate_calibration_correlation(
    profile: CalibrationProfile,
    threshold: float = 0.6,
) -> None:
    """Raise :class:`CalibrationRejectedError` when correlation is below threshold.

    Args:
        profile: Candidate profile produced from a holdout run.
        threshold: Minimum acceptable Pearson correlation. Default 0.6
            matches the R11 PRD.

    Raises:
        CalibrationRejectedError: when ``profile.correlation_coefficient`` is
            strictly less than ``threshold``.
    """
    if profile.correlation_coefficient < threshold:
        raise CalibrationRejectedError(
            f"Correlation {profile.correlation_coefficient:.3f} below "
            f"threshold {threshold:.3f} — profile not emitted"
        )


def emit_profile(
    holdout: Iterable[HoldoutRow],
    *,
    curator_version: str,
    threshold: float = 0.6,
    min_tasks: int = 100,
    min_repos: int = 3,
) -> CalibrationProfile:
    """Run the full R11 gate and return a valid ``CalibrationProfile``.

    Steps (order matters):

    1. ``refuse_profile_emission`` — size and repo-count check.
    2. ``compute_pearson`` — correlation over curator_a vs curator_b.
    3. Construct provisional profile.
    4. ``validate_calibration_correlation`` — threshold check.

    Raises:
        CalibrationRejectedError: at any step that fails.
    """
    rows = tuple(holdout)
    refuse_profile_emission(rows, min_tasks=min_tasks, min_repos=min_repos)

    xs = [row.curator_a for row in rows]
    ys = [row.curator_b for row in rows]
    correlation = compute_pearson(xs, ys)

    distinct_repos = tuple(sorted({row.repo for row in rows}))

    profile = CalibrationProfile(
        correlation_coefficient=correlation,
        holdout_size=len(rows),
        holdout_repos=distinct_repos,
        produced_at=CalibrationProfile.utcnow_iso(),
        curator_version=curator_version,
    )

    validate_calibration_correlation(profile, threshold=threshold)
    return profile


def format_calibration_line(profile: CalibrationProfile | None) -> str:
    """Format a single-line surface of calibration confidence.

    Used by ``codeprobe assess`` and similar read-only surfaces. When
    ``profile`` is ``None`` (no calibration available) returns a clear
    "unavailable" string so downstream UIs never have to special-case it.
    """
    if profile is None:
        return "calibration_confidence: unavailable (no profile loaded)"
    return (
        f"calibration_confidence: {profile.correlation_coefficient:.3f} "
        f"(n={profile.holdout_size}, repos={len(profile.holdout_repos)}, "
        f"curator={profile.curator_version})"
    )
