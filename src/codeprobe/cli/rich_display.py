"""Rich Live terminal dashboard for codeprobe run events.

Provides a :class:`RichLiveListener` that renders a live-updating table
to stderr while an experiment run is in progress.  Thread-safe — designed
to be called from the :class:`EventDispatcher` daemon thread.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from rich.console import Console
from rich.live import Live
from rich.table import Table
from rich.text import Text

from codeprobe.core.events import (
    BudgetWarning,
    RunFinished,
    RunStarted,
    TaskScored,
    TaskStarted,
)

if TYPE_CHECKING:
    from codeprobe.core.events import RunEvent


class RichLiveListener:
    """RunEventListener that renders a Rich Live dashboard to stderr.

    All mutable state is guarded by a lock so updates from the dispatcher
    daemon thread are safe even if ``on_event`` is called concurrently
    (e.g. multiple dispatchers, though the normal case is a single one).
    """

    def __init__(self) -> None:
        self._console = Console(stderr=True)
        self._lock = threading.Lock()

        # Run-level state
        self._total_tasks: int = 0
        self._config_label: str = ""
        self._tasks_completed: int = 0
        self._passed: int = 0
        self._total_cost: float = 0.0
        self._durations: list[float] = []
        self._current_task: str = ""
        self._task_rows: list[tuple[str, str, str, str]] = []
        self._warnings: list[str] = []

        # Rich Live context — created on RunStarted
        self._live: Live | None = None

    # -- public interface (RunEventListener protocol) ----------------------

    def on_event(self, event: RunEvent) -> None:
        """Dispatch *event* to the appropriate handler."""
        if isinstance(event, RunStarted):
            self._handle_run_started(event)
        elif isinstance(event, TaskStarted):
            self._handle_task_started(event)
        elif isinstance(event, TaskScored):
            self._handle_task_scored(event)
        elif isinstance(event, BudgetWarning):
            self._handle_budget_warning(event)
        elif isinstance(event, RunFinished):
            self._handle_run_finished(event)

    # -- handlers ----------------------------------------------------------

    def _handle_run_started(self, event: RunStarted) -> None:
        with self._lock:
            self._total_tasks = event.total_tasks
            self._config_label = event.config_label
            self._tasks_completed = 0
            self._passed = 0
            self._total_cost = 0.0
            self._durations = []
            self._current_task = ""
            self._task_rows = []
            self._warnings = []

        self._live = Live(
            self._build_table(),
            console=self._console,
            refresh_per_second=4,
        )
        self._live.start()

    def _handle_task_started(self, event: TaskStarted) -> None:
        with self._lock:
            self._current_task = event.task_id
        self._refresh()

    def _handle_task_scored(self, event: TaskScored) -> None:
        status = "PASS" if event.automated_score >= 1.0 else "FAIL"
        cost_str = f"${event.cost_usd:.2f}" if event.cost_usd is not None else "n/a"
        duration_str = f"{event.duration_seconds:.1f}s"

        with self._lock:
            self._tasks_completed += 1
            if event.automated_score >= 1.0:
                self._passed += 1
            self._durations.append(event.duration_seconds)
            if event.cost_usd is not None:
                self._total_cost += event.cost_usd
            self._current_task = ""
            self._task_rows.append((event.task_id, status, duration_str, cost_str))
        self._refresh()

    def _handle_budget_warning(self, event: BudgetWarning) -> None:
        pct = int(event.threshold_pct * 100)
        msg = (
            f"Budget {pct}%: ${event.cumulative_cost:.2f} "
            f"of ${event.budget:.2f} used"
        )
        with self._lock:
            self._warnings.append(msg)
        self._refresh()

    def _handle_run_finished(self, event: RunFinished) -> None:
        if self._live is not None:
            # Final update before stopping
            self._live.update(self._build_table())
            self._live.stop()
            self._live = None

        # Print a summary line after the live display is gone
        self._console.print(
            f"[bold]Finished:[/bold] "
            f"{event.completed_count}/{event.total_tasks} tasks, "
            f"mean score {event.mean_score:.2f}, "
            f"total cost ${event.total_cost:.2f}, "
            f"duration {event.total_duration:.1f}s"
        )

    # -- table construction ------------------------------------------------

    def _build_table(self) -> Table:
        """Build a snapshot of the dashboard table.

        Called from ``_refresh()`` which may run on the dispatcher thread.
        All reads of mutable state are under ``self._lock``.
        """
        with self._lock:
            completed = self._tasks_completed
            total = self._total_tasks
            passed = self._passed
            cost = self._total_cost
            durations = list(self._durations)
            current = self._current_task
            label = self._config_label
            rows = list(self._task_rows)
            warnings = list(self._warnings)

        # Header table with progress stats
        table = Table(
            title=f"codeprobe run: {label}",
            show_header=True,
            header_style="bold cyan",
            expand=True,
        )

        table.add_column("Metric", style="bold", width=20)
        table.add_column("Value", width=30)

        # Progress
        table.add_row("Progress", f"{completed}/{total} tasks")

        # Pass rate
        if completed > 0:
            rate = (passed / completed) * 100
            rate_style = "green" if rate >= 80 else "yellow" if rate >= 50 else "red"
            table.add_row(
                "Pass rate",
                Text(f"{rate:.0f}% ({passed}/{completed})", style=rate_style),
            )
        else:
            table.add_row("Pass rate", "-")

        # Cumulative cost
        table.add_row("Cost", f"${cost:.2f}")

        # ETA
        if durations and total > completed:
            avg_duration = sum(durations) / len(durations)
            remaining = total - completed
            eta_seconds = avg_duration * remaining
            if eta_seconds >= 60:
                eta_str = f"{eta_seconds / 60:.1f}m"
            else:
                eta_str = f"{eta_seconds:.0f}s"
            table.add_row("ETA", eta_str)
        elif completed >= total and total > 0:
            table.add_row("ETA", "done")
        else:
            table.add_row("ETA", "-")

        # Current task
        if current:
            table.add_row("Running", Text(current, style="italic"))

        # Warnings
        for warning in warnings:
            style = "bold red" if "100%" in warning else "bold yellow"
            table.add_row("Warning", Text(warning, style=style))

        # Spacer before results
        if rows:
            table.add_section()
            # Show last 10 results to keep the display compact
            visible_rows = rows[-10:]
            for task_id, status, duration, task_cost in visible_rows:
                status_style = "green" if status == "PASS" else "red"
                table.add_row(
                    Text(status, style=status_style),
                    f"{task_id}  {duration}  {task_cost}",
                )
            if len(rows) > 10:
                hidden = len(rows) - 10
                table.add_row(
                    "", Text(f"({hidden} earlier results hidden)", style="dim")
                )

        return table

    # -- helpers -----------------------------------------------------------

    def _refresh(self) -> None:
        """Push an updated table to the Live display if active."""
        if self._live is not None:
            self._live.update(self._build_table())
