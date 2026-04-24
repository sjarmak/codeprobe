"""Retry wrapper for mining operations with a global exhaustion budget.

Per INV1: when the fraction of retry-exhausted attempts crosses
``0.1%`` of total attempted writes the mine aborts. Transient failures
remain silent WARN noise; a sustained failure mode is promoted to an
ERROR-level :class:`RetryLimitExceededError`, which callers surface as a
mine-level abort rather than dropping results on the floor.

The tracker is process-scoped and passed in explicitly — no module
globals — so tests can assert boundary behavior deterministically and
parallel mines against different tenants keep separate budgets.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

logger = logging.getLogger(__name__)

_DEFAULT_RATIO = 0.001  # 0.1%

T = TypeVar("T")


class RetryLimitExceededError(RuntimeError):
    """Raised when the exhausted-to-attempts ratio crosses the threshold.

    Callers treat this as a mine-level ERROR — propagate out of the
    current mine and abort. Do NOT swallow.
    """


_LEGACY_EXCEPTION_ALIASES = {
    "RetryLimitExceeded": "RetryLimitExceededError",
}


def __getattr__(name: str) -> object:
    """Legacy-alias shim — see :mod:`codeprobe.calibration.gate` for rationale."""
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


@dataclass
class RetryTracker:
    """Track attempted and exhausted retry operations.

    Thread-safety: simple int fields; callers using multiple threads
    should wrap ``record_*`` / ``check_threshold`` in an external lock
    if exact ordering matters. For the single-writer mining loops this
    module targets, the built-in GIL suffices.
    """

    ratio: float = _DEFAULT_RATIO
    attempts: int = 0
    exhausted: int = 0
    # Minimum attempts before the ratio check fires — avoids a single
    # early failure tripping the abort on a small run (0 exhausted out
    # of 0 is vacuously fine; 1/1 is not, but firing the abort on the
    # very first failure defeats the purpose of retries).
    min_attempts: int = 100
    history: list[str] = field(default_factory=list)

    def record_attempt(self) -> None:
        """Count one attempted operation (successful or not)."""
        self.attempts += 1

    def record_exhaustion(self, detail: str = "") -> None:
        """Count one operation whose retries were exhausted."""
        self.exhausted += 1
        if detail:
            # Bound memory — keep the last 100 entries so a very long
            # mine doesn't accumulate unbounded failure strings.
            self.history.append(detail)
            if len(self.history) > 100:
                del self.history[:-100]

    def exhausted_ratio(self) -> float:
        """Return exhausted / attempts (0.0 when attempts == 0)."""
        if self.attempts <= 0:
            return 0.0
        return self.exhausted / self.attempts

    def check_threshold(self) -> None:
        """Raise :class:`RetryLimitExceededError` if the ratio is exceeded.

        The check is a *strict* inequality (``ratio > threshold``) so
        exactly 1-in-1000 sits at the boundary and does NOT abort — per
        the PRD's "exceeds 0.1%" wording.
        """
        if self.attempts < self.min_attempts:
            return
        current = self.exhausted_ratio()
        if current > self.ratio:
            msg = (
                f"retry exhaustion {self.exhausted}/{self.attempts} "
                f"({current:.4%}) exceeds threshold {self.ratio:.4%}"
            )
            logger.error("%s — aborting mine", msg)
            raise RetryLimitExceededError(msg)


def retry_call(
    fn: Callable[[], T],
    *,
    tracker: RetryTracker,
    retries: int = 3,
    backoff: float = 0.1,
    exceptions: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Callable[[int, BaseException], None] | None = None,
) -> T:
    """Call *fn* with bounded retries, tracking attempts + exhaustion.

    Behavior:

    - Each call (including the first) counts as one attempt against
      *tracker*.
    - On success, return the result.
    - On failure, sleep ``backoff * 2**attempt`` and retry up to
      *retries* times.
    - When retries are exhausted, increment ``tracker.exhausted``, call
      ``tracker.check_threshold()`` (which may raise
      :class:`RetryLimitExceededError`), and re-raise the original exception.
    - Any exception that isn't in *exceptions* propagates immediately
      without burning retries.

    Callers that want per-call retry without a shared tracker should
    construct a throwaway :class:`RetryTracker` instance.
    """
    if retries < 0:
        raise ValueError("retries must be >= 0")

    last_exc: BaseException | None = None
    for attempt in range(retries + 1):
        tracker.record_attempt()
        try:
            return fn()
        except exceptions as exc:
            last_exc = exc
            if attempt >= retries:
                tracker.record_exhaustion(detail=repr(exc))
                logger.warning(
                    "retry exhausted after %d attempts: %s", attempt + 1, exc
                )
                tracker.check_threshold()
                raise
            if on_retry is not None:
                try:
                    on_retry(attempt, exc)
                except Exception:  # pragma: no cover - callback defensive
                    logger.debug("on_retry callback raised", exc_info=True)
            sleep_s = backoff * (2**attempt)
            logger.debug(
                "retry %d/%d after %.3fs: %s",
                attempt + 1,
                retries,
                sleep_s,
                exc,
            )
            if sleep_s > 0:
                time.sleep(sleep_s)

    # Unreachable — the loop either returns or raises — but keep the
    # type checker happy.
    raise RuntimeError("retry_call exhausted without raising") from last_exc
