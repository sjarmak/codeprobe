"""codeprobe interpret — analyze results and produce recommendations."""

from __future__ import annotations

from pathlib import Path

import click


def _count_expected_tasks(tasks_dir: Path) -> int | None:
    """Count the number of task directories in the tasks manifest.

    Returns None if the tasks directory does not exist.
    Each subdirectory containing an ``instruction.md`` is counted as a task.
    """
    if not tasks_dir.is_dir():
        return None

    count = sum(
        1
        for child in tasks_dir.iterdir()
        if child.is_dir() and (child / "instruction.md").is_file()
    )
    return count if count > 0 else None


def run_interpret(path: str, fmt: str = "text") -> None:
    """Analyze eval results and generate report."""
    from codeprobe.analysis import (
        format_csv_report,
        format_json_report,
        format_text_report,
        generate_report,
    )
    from codeprobe.core.experiment import load_config_results, load_experiment

    exp_dir = Path(path).resolve()
    experiment = load_experiment(exp_dir)

    all_results = []
    for config in experiment.configs:
        try:
            results = load_config_results(exp_dir, config.label)
            all_results.append(results)
        except (FileNotFoundError, ValueError) as exc:
            click.echo(f"Warning: Skipping config '{config.label}': {exc}")

    if not all_results:
        click.echo("No results found. Run 'codeprobe run' first.")
        return

    # Detect incomplete sweeps by comparing checkpoint results to task manifest
    total_tasks = _count_expected_tasks(exp_dir / experiment.tasks_dir)

    report = generate_report(experiment.name, all_results, total_tasks=total_tasks)

    if report.is_partial:
        click.echo(
            f"Note: Sweep incomplete — "
            f"{report.completion_ratio:.0%} of tasks finished. "
            f"Results are partial.\n"
        )

    if fmt == "csv":
        click.echo(format_csv_report(report))
    elif fmt == "json":
        click.echo(format_json_report(report))
    else:
        click.echo(format_text_report(report))
