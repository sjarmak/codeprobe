"""Typed event dataclasses and queue-based event dispatcher.

Provides a publish-subscribe event system for run lifecycle events.
Events are dispatched asynchronously via a daemon thread so callers
(the executor hot path) are never blocked by listener processing.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Protocol, Union, runtime_checkable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event types (all frozen — immutable after creation)
# ---------------------------------------------------------------------------

_SENTINEL = object()


@dataclass(frozen=True)
class RunStarted:
    """Emitted once at the beginning of an experiment run."""

    total_tasks: int
    config_label: str
    timestamp: float


@dataclass(frozen=True)
class TaskStarted:
    """Emitted when a single task begins execution."""

    task_id: str
    config_label: str
    timestamp: float


@dataclass(frozen=True)
class TaskScored:
    """Emitted after a task completes and is scored."""

    task_id: str
    config_label: str
    automated_score: float
    duration_seconds: float
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cost_model: str
    cost_source: str
    error: str | None
    timestamp: float


@dataclass(frozen=True)
class BudgetWarning:
    """Emitted when cumulative cost crosses a warning threshold."""

    cumulative_cost: float
    budget: float
    threshold_pct: float
    timestamp: float


@dataclass(frozen=True)
class RunFinished:
    """Emitted once at the end of an experiment run."""

    total_tasks: int
    completed_count: int
    mean_score: float
    total_cost: float
    total_duration: float
    config_label: str
    timestamp: float


# ---------------------------------------------------------------------------
# Union type and listener protocol
# ---------------------------------------------------------------------------

RunEvent = Union[RunStarted, TaskStarted, TaskScored, BudgetWarning, RunFinished]


@runtime_checkable
class RunEventListener(Protocol):
    """Protocol for objects that consume run lifecycle events."""

    def on_event(self, event: RunEvent) -> None: ...


# ---------------------------------------------------------------------------
# EventDispatcher — async queue-based fan-out
# ---------------------------------------------------------------------------


class EventDispatcher:
    """Queue-based event dispatcher with a daemon dispatch thread.

    Listeners are called sequentially on the daemon thread so the caller
    of ``emit()`` is never blocked.  Call ``shutdown()`` to drain the
    queue and join the dispatch thread.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[RunEvent | object] = queue.Queue()
        self._listeners: list[RunEventListener] = []
        self._thread = threading.Thread(
            target=self._dispatch_loop,
            daemon=True,
            name="event-dispatcher",
        )
        self._thread.start()

    # -- public API --------------------------------------------------------

    def register(self, listener: RunEventListener) -> None:
        """Add a listener that will receive all future events."""
        self._listeners.append(listener)

    def emit(self, event: RunEvent) -> None:
        """Enqueue *event* for asynchronous delivery — non-blocking."""
        self._queue.put(event)

    def shutdown(self) -> None:
        """Signal the dispatch thread to drain remaining events and stop.

        Blocks until the thread has processed every queued event.
        """
        self._queue.put(_SENTINEL)
        self._thread.join()

    # -- internal ----------------------------------------------------------

    def _dispatch_loop(self) -> None:
        """Drain the queue and fan out each event to all listeners.

        After the sentinel is received the loop does one final drain pass
        so that events emitted *by listeners* during the last batch (e.g.
        BudgetWarning from BudgetChecker) are still delivered.
        """
        while True:
            item = self._queue.get()
            if item is _SENTINEL:
                # Final drain: process any events added by listeners
                self._drain_remaining()
                break
            self._deliver(item)  # type: ignore[arg-type]

    def _deliver(self, event: RunEvent) -> None:
        for listener in self._listeners:
            try:
                listener.on_event(event)
            except Exception:
                logger.exception(
                    "Listener %r raised on event %s",
                    listener,
                    type(event).__name__,
                )

    def _drain_remaining(self) -> None:
        """Deliver any events still in the queue (non-blocking)."""
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _SENTINEL:
                continue
            self._deliver(item)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# BudgetChecker — listener that tracks cumulative cost
# ---------------------------------------------------------------------------

_BILLABLE_COST_MODELS = frozenset({"per_token"})


class BudgetChecker:
    """Tracks cumulative cost and emits warnings at threshold crossings.

    Implements :class:`RunEventListener`.  Only ``TaskScored`` events
    with a billable ``cost_model`` (currently ``per_token``) contribute
    to the running total.

    Parameters
    ----------
    budget:
        Maximum allowed cost in USD.
    warning_threshold:
        Fraction of *budget* at which a :class:`BudgetWarning` is emitted
        (default 0.8 = 80%).
    """

    def __init__(
        self,
        budget: float,
        warning_threshold: float = 0.8,
    ) -> None:
        self._budget = budget
        self._warning_threshold = warning_threshold
        self._cumulative_cost = 0.0
        self._lock = threading.Lock()
        self._exceeded = threading.Event()
        self._warning_emitted = False
        self._exceeded_emitted = False
        self._dispatcher: EventDispatcher | None = None

    # -- wiring ------------------------------------------------------------

    def set_dispatcher(self, dispatcher: EventDispatcher) -> None:
        """Provide a back-reference so BudgetChecker can emit warnings."""
        self._dispatcher = dispatcher

    # -- properties --------------------------------------------------------

    @property
    def cumulative_cost(self) -> float:
        with self._lock:
            return self._cumulative_cost

    @property
    def is_exceeded(self) -> bool:
        return self._exceeded.is_set()

    # -- listener ----------------------------------------------------------

    def on_event(self, event: RunEvent) -> None:
        """Process a run event, accumulating cost from scored tasks."""
        if not isinstance(event, TaskScored):
            return
        if event.cost_model not in _BILLABLE_COST_MODELS:
            return
        if event.cost_usd is None:
            return

        with self._lock:
            self._cumulative_cost += event.cost_usd
            current = self._cumulative_cost

        # 80% warning
        if (
            not self._warning_emitted
            and current >= self._budget * self._warning_threshold
        ):
            self._warning_emitted = True
            self._emit_warning(current, self._warning_threshold)

        # 100% exceeded
        if current >= self._budget:
            self._exceeded.set()
            if not self._exceeded_emitted:
                self._exceeded_emitted = True
                self._emit_warning(current, 1.0)

    # -- internal ----------------------------------------------------------

    def _emit_warning(self, current: float, threshold_pct: float) -> None:
        if self._dispatcher is None:
            return
        warning = BudgetWarning(
            cumulative_cost=current,
            budget=self._budget,
            threshold_pct=threshold_pct,
            timestamp=time.time(),
        )
        self._dispatcher.emit(warning)
