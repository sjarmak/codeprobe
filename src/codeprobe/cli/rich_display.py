"""Rich Live terminal dashboard for codeprobe run events.

Provides a :class:`RichLiveListener` that renders a live-updating table
to stderr while an experiment run is in progress.  Thread-safe — designed
to be called from the :class:`EventDispatcher` daemon thread.

When multiple configs run in parallel, a single :class:`RichLiveListener`
instance should be shared across all dispatchers so that one ``Live``
context manages the terminal.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

from codeprobe.analysis.dual import format_dual_suffix
from codeprobe.analysis.stats import score_passed
from codeprobe.core.events import (
    BudgetWarning,
    RunFinished,
    RunStarted,
    TaskScored,
    TaskStarted,
)

if TYPE_CHECKING:
    from codeprobe.core.events import RunEvent


@dataclass
class _ConfigState:
    """Per-config mutable state, guarded by the listener's lock."""

    label: str
    total_tasks: int = 0
    tasks_completed: int = 0
    passed: int = 0
    total_cost: float = 0.0
    durations: list[float] = field(default_factory=list)
    current_task: str = ""
    task_rows: list[tuple[str, str, str, str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    finished: bool = False


class RichLiveListener:
    """RunEventListener that renders a Rich Live dashboard to stderr.

    Supports multiple configs rendered in a single Live display.
    All mutable state is guarded by a lock so updates from dispatcher
    daemon threads are safe.
    """

    def __init__(self) -> None:
        self._console = Console(stderr=True)
        self._lock = threading.Lock()

        # Keyed by config_label
        self._configs: dict[str, _ConfigState] = {}
        self._config_order: list[str] = []

        # Track how many configs are expected vs finished
        self._active_count = 0

        # Rich Live context — created on first RunStarted
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

    def _get_or_create_config(self, label: str) -> _ConfigState:
        """Return existing config state or create a new one (caller holds lock)."""
        if label not in self._configs:
            state = _ConfigState(label=label)
            self._configs[label] = state
            self._config_order.append(label)
        return self._configs[label]

    def _handle_run_started(self, event: RunStarted) -> None:
        start_live = False
        with self._lock:
            state = self._get_or_create_config(event.config_label)
            state.total_tasks = event.total_tasks
            self._active_count += 1
            if self._live is None:
                start_live = True

        if start_live:
            self._live = Live(
                self._build_display(),
                console=self._console,
                refresh_per_second=4,
            )
            self._live.start()
        else:
            self._refresh()

    def _handle_task_started(self, event: TaskStarted) -> None:
        with self._lock:
            state = self._get_or_create_config(event.config_label)
            state.current_task = event.task_id
        self._refresh()

    @staticmethod
    def _format_score(score: float) -> str:
        if score >= 1.0:
            return "PASS"
        if score <= 0.0:
            return "FAIL"
        return f"{score:.2f}"

    def _handle_task_scored(self, event: TaskScored) -> None:
        status = self._format_score(event.automated_score)
        cost_str = f"${event.cost_usd:.2f}" if event.cost_usd is not None else "n/a"
        duration_str = f"{event.duration_seconds:.1f}s"
        dual_suffix = format_dual_suffix(event.scoring_details)

        with self._lock:
            state = self._get_or_create_config(event.config_label)
            state.tasks_completed += 1
            if score_passed(event.automated_score, event.scoring_details):
                state.passed += 1
            state.durations.append(event.duration_seconds)
            if event.cost_usd is not None:
                state.total_cost += event.cost_usd
            state.current_task = ""
            state.task_rows.append(
                (event.task_id, status, duration_str, cost_str, dual_suffix)
            )
        self._refresh()

    def _handle_budget_warning(self, event: BudgetWarning) -> None:
        pct = int(event.threshold_pct * 100)
        msg = (
            f"Budget {pct}%: ${event.cumulative_cost:.2f} "
            f"of ${event.budget:.2f} used"
        )
        with self._lock:
            # Budget warnings are global — add to all active configs
            for state in self._configs.values():
                if not state.finished:
                    state.warnings.append(msg)
        self._refresh()

    def _handle_run_finished(self, event: RunFinished) -> None:
        all_done = False
        with self._lock:
            self._active_count -= 1
            if event.config_label in self._configs:
                self._configs[event.config_label].finished = True
            all_done = self._active_count <= 0

        if all_done and self._live is not None:
            # Final update before stopping
            self._live.update(self._build_display())
            self._live.stop()
            self._live = None

            # Print summary lines for each config
            with self._lock:
                for label in self._config_order:
                    state = self._configs[label]
                    total_dur = sum(state.durations)
                    mean_score = (
                        (state.passed / state.tasks_completed)
                        if state.tasks_completed > 0
                        else 0.0
                    )
                    self._console.print(
                        f"[bold]Finished {state.label}:[/bold] "
                        f"{state.tasks_completed}/{state.total_tasks} tasks, "
                        f"mean score {mean_score:.2f}, "
                        f"total cost ${state.total_cost:.2f}, "
                        f"duration {total_dur:.1f}s"
                    )
        else:
            self._refresh()

    # -- display construction ----------------------------------------------

    def _build_display(self) -> Group:
        """Build a snapshot of all config tables as a single renderable."""
        with self._lock:
            configs = [(label, self._configs[label]) for label in self._config_order]

        renderables = []
        for _label, state in configs:
            renderables.append(self._build_config_table(state))

        return Group(*renderables)

    def _build_config_table(self, state: _ConfigState) -> Table:
        """Build a table for a single config's state.

        Caller must NOT hold ``self._lock`` — this method reads only from
        the passed *state* snapshot fields.
        """
        completed = state.tasks_completed
        total = state.total_tasks
        passed = state.passed
        durations = list(state.durations)
        current = state.current_task
        label = state.label
        rows = list(state.task_rows)
        warnings = list(state.warnings)

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
            visible_rows = rows[-10:]
            for task_id, status, duration, _task_cost, dual_suffix in visible_rows:
                status_style = "green" if status == "PASS" else "red"
                table.add_row(
                    Text(status, style=status_style),
                    f"{task_id}  {duration}{dual_suffix}",
                )
            if len(rows) > 10:
                hidden = len(rows) - 10
                table.add_row(
                    "", Text(f"({hidden} earlier results hidden)", style="dim")
                )

        return table

    # -- helpers -----------------------------------------------------------

    def _refresh(self) -> None:
        """Push an updated display to the Live context if active."""
        if self._live is not None:
            self._live.update(self._build_display())
