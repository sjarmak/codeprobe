"""codeprobe interpret — analyze results and produce recommendations."""

from __future__ import annotations

from pathlib import Path

import click

from codeprobe.cli._output_helpers import emit_envelope, resolve_mode
from codeprobe.cli.errors import PrescriptiveError
from codeprobe.config.defaults import (
    resolve_experiment_config,
    use_v07_defaults,
)


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


def run_regression(path: str, results_path: str | None = None) -> None:
    """Print a per-task score-over-commits regression report.

    Walks ``path`` looking for a tasks directory (``<path>/tasks`` or
    ``<path>`` itself if it contains metadata.json files) and renders a
    rich table grouped by ``task.id``. Scores are read from
    ``results_path`` when provided (see
    :func:`codeprobe.analysis.interpret.collect_task_regressions` for the
    accepted layouts).

    This path intentionally does NOT require a full experiment.json —
    regression plotting works on a mined task directory alone, which is
    the common shape users interact with after ``codeprobe mine --refresh``.
    """
    from codeprobe.analysis.interpret import regression_report

    root = Path(path).resolve()

    # Accept either a tasks/ directory directly, or an experiment root
    # whose tasks live under ./tasks/.
    candidate = root / "tasks"
    tasks_dir = candidate if candidate.is_dir() else root

    results_dir = Path(results_path).resolve() if results_path else None

    report = regression_report(tasks_dir, results_dir=results_dir)
    click.echo(report)


def run_interpret(
    path: str,
    fmt: str = "text",
    *,
    json_flag: bool = False,
    no_json_flag: bool = False,
    json_lines_flag: bool = False,
) -> None:
    """Analyze eval results and generate report."""
    import json as _json

    from codeprobe.analysis import (
        format_csv_report,
        format_html_report,
        format_json_report,
        format_text_report,
        generate_report,
    )
    from codeprobe.core.experiment import load_config_results, load_experiment

    # Resolve output mode once up-front. ``--format text`` maps to an
    # explicit pretty request; ``--format json`` is orthogonal and is used
    # to produce a report payload inside the envelope when envelope mode is
    # active.
    mode = resolve_mode(
        "interpret",
        json_flag,
        no_json_flag,
        json_lines_flag,
        explicit_format="text" if fmt == "text" else None,
    )

    exp_dir = Path(path).resolve()

    # Under v0.7, attempt the deterministic auto-discovery resolver
    # before falling back to the pre-PRD discovery logic below. When
    # the resolver raises AMBIGUOUS_EXPERIMENT we still let the classic
    # loader try — it handles explicit paths fine — and surface a
    # prescriptive message if everything fails.
    if use_v07_defaults():
        try:
            resolved, _ = resolve_experiment_config(exp_dir)
            exp_dir = resolved.parent
        except PrescriptiveError:
            pass

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError):
        # Try .codeprobe/ directory (experiment init puts it there)
        codeprobe_dir = exp_dir / ".codeprobe"
        if codeprobe_dir.is_dir() and (codeprobe_dir / "experiment.json").is_file():
            exp_dir = codeprobe_dir
            experiment = load_experiment(exp_dir)
        else:
            raise

    all_results = []
    for config in experiment.configs:
        try:
            results = load_config_results(exp_dir, config.label)
            all_results.append(results)
        except (FileNotFoundError, ValueError) as exc:
            if mode.mode == "pretty":
                click.echo(f"Warning: Skipping config '{config.label}': {exc}")

    if not all_results:
        if mode.mode == "pretty":
            click.echo("No results found. Run 'codeprobe run' first.")
        else:
            emit_envelope(
                command="interpret",
                data={
                    "experiment": experiment.name,
                    "has_results": False,
                    "message": "No results found. Run 'codeprobe run' first.",
                },
            )
        return

    # Detect incomplete sweeps by comparing checkpoint results to task manifest
    total_tasks = _count_expected_tasks(exp_dir / experiment.tasks_dir)

    report = generate_report(experiment.name, all_results, total_tasks=total_tasks)

    if mode.mode == "pretty":
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
        elif fmt == "html":
            html = format_html_report(report)
            out_path = exp_dir / f"{experiment.name}_report.html"
            out_path.write_text(html)
            click.echo(f"HTML report written to {out_path}")
        else:
            click.echo(format_text_report(report))
        return

    # Envelope / NDJSON mode — serialize the report payload into data.
    report_json = format_json_report(report)
    try:
        report_payload = _json.loads(report_json)
    except ValueError:
        report_payload = {"raw": report_json}

    emit_envelope(
        command="interpret",
        data={
            "experiment": experiment.name,
            "has_results": True,
            "is_partial": report.is_partial,
            "completion_ratio": report.completion_ratio,
            "report": report_payload,
        },
    )
