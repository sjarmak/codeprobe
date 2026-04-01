"""codeprobe mine — extract eval tasks from repo history."""

from __future__ import annotations

import shutil
import sys

import click


def _discover_and_select(
    repo_path: "Path",
    source_hint: str,
) -> tuple[str, ...]:
    """List subsystems from merge history and let the user pick.

    Returns selected subsystem prefixes. Falls back to top 3 in
    non-interactive environments.
    """
    from pathlib import Path

    from codeprobe.mining import extract_subsystems
    from codeprobe.mining.extractor import list_merged_prs
    from codeprobe.mining.sources import detect_source, RepoSource

    if source_hint != "auto":
        source = RepoSource(
            host=source_hint, owner="", repo=repo_path.name, remote_url=""
        )
    else:
        source = detect_source(repo_path)

    prs = list_merged_prs(source, repo_path, limit=40)
    if not prs:
        click.echo("No merge commits found — cannot discover subsystems.")
        return ()

    subsystem_counts = extract_subsystems(prs, repo_path)
    if not subsystem_counts:
        click.echo("No subsystems detected in merge history.")
        return ()

    # Display the subsystem table
    entries = list(subsystem_counts.items())[:20]
    click.echo()
    click.echo("Subsystems (by merge activity):")
    for i, (prefix, count) in enumerate(entries, 1):
        click.echo(f"  [{i:2d}] {prefix:40s} ({count} merges)")
    click.echo()

    # Non-interactive fallback: pick top 3
    if not sys.stdin.isatty():
        top = tuple(p for p, _ in entries[:3])
        click.echo(f"Non-interactive: auto-selected {', '.join(top)}")
        return top

    raw = click.prompt(
        "Select subsystems (comma-separated numbers, or Enter for top 3)",
        default="",
        show_default=False,
    )

    if not raw.strip():
        return tuple(p for p, _ in entries[:3])

    selected: list[str] = []
    for token in raw.split(","):
        token = token.strip()
        try:
            idx = int(token)
            if 1 <= idx <= len(entries):
                selected.append(entries[idx - 1][0])
            else:
                click.echo(f"  Skipping out-of-range index: {idx}")
        except ValueError:
            # Treat as a literal prefix
            if not token.endswith("/"):
                token += "/"
            selected.append(token)

    if not selected:
        return tuple(p for p, _ in entries[:3])

    return tuple(selected)


def run_mine(
    path: str,
    count: int = 5,
    source: str = "auto",
    min_files: int = 0,
    subsystems: tuple[str, ...] = (),
    discover_subsystems: bool = False,
) -> None:
    """Mine eval tasks from a repository."""
    from pathlib import Path

    from codeprobe.mining import mine_tasks, write_task_dir

    repo_path = Path(path).resolve()

    # Subsystem discovery: list available subsystems, let user pick
    if discover_subsystems:
        subsystems = _discover_and_select(repo_path, source)
        if not subsystems:
            return

    # Normalize prefixes to end with /
    subsystems = tuple(s if s.endswith("/") else s + "/" for s in subsystems)

    tasks = mine_tasks(
        repo_path,
        count=count,
        source_hint=source,
        min_files=min_files,
        subsystems=subsystems,
    )

    if not tasks:
        click.echo(
            "No suitable tasks found. Try a repo with merged PRs that include tests."
        )
        return

    # Clear stale tasks from prior runs before writing new ones
    tasks_dir = repo_path / ".codeprobe" / "tasks"
    if tasks_dir.exists():
        shutil.rmtree(tasks_dir)

    for task in tasks:
        write_task_dir(task, tasks_dir, repo_path)

    click.echo(f"Mined {len(tasks)} tasks → {tasks_dir}")
    if subsystems:
        click.echo(f"Subsystems: {', '.join(subsystems)}")
    click.echo()
    click.echo("Next: codeprobe run . --agent claude")
