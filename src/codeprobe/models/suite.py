"""Suite data model — a curated collection of tasks with filtering criteria."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Suite:
    """A suite manifest that filters tasks by type, difficulty, and eval goal.

    Both mine and probe generators contribute tasks; the run command
    consumes suites to scope benchmark runs.
    """

    name: str
    description: str = ""
    task_dir: str = "tasks"
    task_types: tuple[str, ...] = ()
    difficulties: tuple[str, ...] = ()
    eval_goal: str = ""
    tags: tuple[str, ...] = ()
    task_ids: tuple[str, ...] = ()
