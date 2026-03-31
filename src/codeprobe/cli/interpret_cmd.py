"""codeprobe interpret — analyze results and produce recommendations."""

from __future__ import annotations

import click


def run_interpret(path: str, fmt: str = "text") -> None:
    """Analyze eval results and generate report."""
    from pathlib import Path as P

    from codeprobe.analysis import format_json_report, format_text_report, generate_report
    from codeprobe.core.experiment import load_config_results, load_experiment

    exp_dir = P(path).resolve()
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

    report = generate_report(experiment.name, all_results)

    if fmt == "json":
        click.echo(format_json_report(report))
    else:
        click.echo(format_text_report(report))
