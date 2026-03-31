"""codeprobe mine — extract eval tasks from repo history."""

from __future__ import annotations

import click


def run_mine(path: str, count: int = 5, source: str = "auto") -> None:
    """Mine eval tasks from a repository."""
    from pathlib import Path

    from codeprobe.mining import mine_tasks, write_task_dir

    repo_path = Path(path).resolve()
    tasks = mine_tasks(repo_path, count=count, source_hint=source)

    if not tasks:
        click.echo("No suitable tasks found. Try a repo with merged PRs that include tests.")
        return

    tasks_dir = repo_path / ".codeprobe" / "tasks"
    for task in tasks:
        write_task_dir(task, tasks_dir, repo_path)

    click.echo(f"Mined {len(tasks)} tasks → {tasks_dir}")
    click.echo()
    click.echo("Next: codeprobe run . --agent claude")
