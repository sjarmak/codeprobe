"""codeprobe experiment — manage eval experiments."""

from __future__ import annotations

import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

import click

from codeprobe.core.experiment import (
    create_experiment_dir,
    load_config_results,
    load_experiment,
    save_experiment,
)
from codeprobe.models.experiment import (
    Experiment,
    ExperimentConfig,
)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def experiment_init(
    path: str,
    name: str,
    description: str,
) -> None:
    """Create a new experiment directory."""
    base_dir = Path(path)
    exp_dir = base_dir / name

    if exp_dir.exists():
        click.echo(f"Error: experiment '{name}' already exists at {exp_dir}", err=True)
        raise SystemExit(1)

    experiment = Experiment(
        name=name,
        description=description,
    )

    try:
        created = create_experiment_dir(base_dir, experiment)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    click.echo(f"Experiment '{name}' created at {created}/")
    click.echo("  Tasks: 0 (add tasks to tasks/ directory)")
    click.echo(
        "  Configs: 0 (use 'codeprobe experiment add-config' to define configurations)"
    )


def _interactive_mcp_selection() -> str | None:
    """Offer interactive MCP config selection when available.

    Returns a file path string if the user selects a config, or None to skip.
    """
    from codeprobe.core.mcp_discovery import discover_mcp_configs

    discovered = discover_mcp_configs()
    if not discovered:
        return None

    click.echo()
    click.echo("Discovered MCP configurations:")
    for i, (p, servers) in enumerate(discovered, 1):
        click.echo(f"  {i}. {p}  ({len(servers)} servers)")
        for s in servers:
            click.echo(f"     - {s}")
    click.echo(f"  {len(discovered) + 1}. Skip (no MCP config)")
    click.echo()

    choice = click.prompt(
        "Select MCP config",
        type=click.IntRange(1, len(discovered) + 1),
        default=len(discovered) + 1,
    )
    if choice <= len(discovered):
        return str(discovered[choice - 1][0])
    return None


def experiment_add_config(
    path: str,
    label: str,
    agent: str,
    model: str | None,
    permission_mode: str,
    mcp_config_str: str | None,
    instruction_variant: str | None = None,
    preambles: tuple[str, ...] = (),
) -> None:
    """Add a configuration to an existing experiment."""
    exp_dir = Path(path)

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    # Check for duplicate label
    existing_labels = [c.label for c in experiment.configs]
    if label in existing_labels:
        click.echo(
            f"Error: configuration '{label}' already exists in experiment '{experiment.name}'",
            err=True,
        )
        raise SystemExit(1)

    # Parse MCP config — offer interactive discovery when omitted in a TTY
    mcp_config: dict | None = None
    if mcp_config_str:
        try:
            mcp_config = json.loads(mcp_config_str)
        except json.JSONDecodeError:
            mcp_path = Path(mcp_config_str).expanduser().resolve()
            if mcp_path.is_file():
                mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))
            else:
                click.echo(
                    "Error: --mcp-config is not valid JSON or a file path",
                    err=True,
                )
                raise SystemExit(1)
    elif sys.stderr.isatty():
        mcp_config_str = _interactive_mcp_selection()
        if mcp_config_str:
            mcp_path = Path(mcp_config_str).expanduser().resolve()
            if mcp_path.is_file():
                mcp_config = json.loads(mcp_path.read_text(encoding="utf-8"))

    new_config = ExperimentConfig(
        label=label,
        agent=agent,
        model=model,
        permission_mode=permission_mode,
        mcp_config=mcp_config,
        instruction_variant=instruction_variant,
        preambles=preambles,
    )

    # Validate the label is a safe path component
    from codeprobe.core.experiment import _validate_path_component

    try:
        _validate_path_component(label, "config label")
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    updated = Experiment(
        name=experiment.name,
        description=experiment.description,
        configs=[*experiment.configs, new_config],
        tasks_dir=experiment.tasks_dir,
    )
    save_experiment(exp_dir, updated)

    # Create runs directory for this config
    (exp_dir / "runs" / label).mkdir(parents=True, exist_ok=True)

    click.echo(f"Configuration '{label}' added to experiment '{experiment.name}'")
    click.echo(f"  Agent: {agent}")
    click.echo(f"  Model: {model or '(not specified)'}")
    click.echo(f"  Total configs: {len(updated.configs)}")


def experiment_validate(path: str) -> None:
    """Validate experiment structure and readiness."""
    exp_dir = Path(path)

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    errors: list[str] = []
    warnings: list[str] = []

    # Discover tasks from the tasks directory
    tasks_dir = exp_dir / experiment.tasks_dir
    task_ids: list[str] = []
    if tasks_dir.is_dir():
        task_ids = sorted(
            d.name
            for d in tasks_dir.iterdir()
            if d.is_dir() and (d / "instruction.md").exists()
        )

    if not task_ids:
        errors.append("No tasks found. Add task directories to tasks/.")

    for task_id in task_ids:
        task_dir = tasks_dir / task_id
        if not (task_dir / "tests" / "test.sh").exists():
            warnings.append(
                f"Task '{task_id}' has no tests/test.sh (automated scoring unavailable)"
            )

    if not experiment.configs:
        errors.append(
            "No configurations defined. Use 'add-config' to add at least one."
        )

    total_runs = len(task_ids) * len(experiment.configs)

    click.echo(f"Experiment: {experiment.name}")
    click.echo(f"  Tasks: {len(task_ids)}")
    click.echo(f"  Configurations: {len(experiment.configs)}")
    click.echo(f"  Total runs needed: {total_runs}")

    if errors:
        click.echo(f"\n  ERRORS ({len(errors)}):")
        for e in errors:
            click.echo(f"    - {e}")
    if warnings:
        click.echo(f"\n  WARNINGS ({len(warnings)}):")
        for w in warnings:
            click.echo(f"    - {w}")

    if not errors and not warnings:
        click.echo("\n  Status: READY to run")
    elif not errors:
        click.echo(f"\n  Status: READY (with {len(warnings)} warnings)")
    else:
        click.echo(f"\n  Status: NOT READY ({len(errors)} errors)")
        raise SystemExit(1)


def experiment_status(path: str) -> None:
    """Report completion status per configuration."""
    exp_dir = Path(path)

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    tasks_dir = exp_dir / experiment.tasks_dir
    task_ids: list[str] = []
    if tasks_dir.is_dir():
        task_ids = sorted(
            d.name
            for d in tasks_dir.iterdir()
            if d.is_dir() and (d / "instruction.md").exists()
        )

    total_tasks = len(task_ids)

    click.echo(f"Experiment: {experiment.name}")
    click.echo(f"  Description: {experiment.description}")
    click.echo(f"  Tasks: {total_tasks}")
    click.echo()

    if not experiment.configs:
        click.echo("  No configurations defined yet.")
        return

    click.echo(
        f"  {'Configuration':<25} {'Completed':<12} {'Score (avg)':<12} {'Status'}"
    )
    click.echo(f"  {'-' * 25} {'-' * 12} {'-' * 12} {'-' * 10}")

    for cfg in experiment.configs:
        completed = 0
        avg_score: float | None = None

        try:
            results = load_config_results(exp_dir, cfg.label)
            completed = len(results.completed)
            scores = [
                t.automated_score
                for t in results.completed
                if t.automated_score is not None
            ]
            if scores:
                avg_score = statistics.mean(scores)
        except FileNotFoundError:
            pass

        score_str = f"{avg_score:.2f}" if avg_score is not None else "--"
        status_str = (
            "complete" if completed == total_tasks and total_tasks > 0 else "pending"
        )
        progress = f"{completed}/{total_tasks}" if completed < total_tasks else "done"
        click.echo(f"  {cfg.label:<25} {progress:<12} {score_str:<12} {status_str}")


def experiment_aggregate(path: str) -> None:
    """Aggregate results across configurations into a comparison report."""
    exp_dir = Path(path)

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    if len(experiment.configs) < 1:
        click.echo(
            "Error: need at least 1 configuration with results to aggregate",
            err=True,
        )
        raise SystemExit(1)

    # Load results for each config
    config_results: dict[str, list[dict]] = {}
    for cfg in experiment.configs:
        try:
            results = load_config_results(exp_dir, cfg.label)
            config_results[cfg.label] = [
                {
                    "task_id": t.task_id,
                    "automated_score": t.automated_score,
                    "duration_seconds": t.duration_seconds,
                    "cost_usd": t.cost_usd,
                }
                for t in results.completed
            ]
        except FileNotFoundError:
            config_results[cfg.label] = []

    # Per-config summaries
    config_summaries: dict[str, dict] = {}
    for cfg_label, results in config_results.items():
        scores = [
            r["automated_score"] for r in results if r["automated_score"] is not None
        ]
        costs = [r["cost_usd"] for r in results if r.get("cost_usd") is not None]
        times = [
            r["duration_seconds"]
            for r in results
            if r.get("duration_seconds") is not None
        ]

        config_summaries[cfg_label] = {
            "tasks_completed": len(results),
            "mean_automated_score": (statistics.mean(scores) if scores else None),
            "stdev_automated_score": (
                statistics.stdev(scores) if len(scores) > 1 else None
            ),
            "total_cost_usd": sum(costs) if costs else None,
            "mean_cost_per_task": (statistics.mean(costs) if costs else None),
            "total_time_seconds": sum(times) if times else None,
            "score_per_dollar": (
                statistics.mean(scores) / statistics.mean(costs)
                if scores and costs and statistics.mean(costs) > 0
                else None
            ),
        }

    # Pairwise deltas
    config_labels = [c.label for c in experiment.configs]
    pairwise: list[dict] = []
    for i, a_label in enumerate(config_labels):
        for b_label in config_labels[i + 1 :]:
            a_scores = {
                r["task_id"]: r["automated_score"]
                for r in config_results.get(a_label, [])
                if r["automated_score"] is not None
            }
            b_scores = {
                r["task_id"]: r["automated_score"]
                for r in config_results.get(b_label, [])
                if r["automated_score"] is not None
            }
            shared = set(a_scores) & set(b_scores)
            if shared:
                deltas = [b_scores[t] - a_scores[t] for t in shared]
                mean_delta = statistics.mean(deltas)
                wins_b = sum(1 for d in deltas if d > 0.01)
                wins_a = sum(1 for d in deltas if d < -0.01)
                ties = len(deltas) - wins_b - wins_a

                cohens_d: float | None = None
                if len(deltas) > 1:
                    sd = statistics.stdev(deltas)
                    cohens_d = mean_delta / sd if sd > 0 else None

                pairwise.append(
                    {
                        "config_a": a_label,
                        "config_b": b_label,
                        "shared_tasks": len(shared),
                        "mean_delta": round(mean_delta, 4),
                        "wins_a": wins_a,
                        "wins_b": wins_b,
                        "ties": ties,
                        "cohens_d": (
                            round(cohens_d, 3) if cohens_d is not None else None
                        ),
                    }
                )

    aggregate = {
        "experiment": experiment.name,
        "generated": _now_iso(),
        "config_count": len(experiment.configs),
        "config_summaries": config_summaries,
        "pairwise_deltas": pairwise,
    }

    reports_dir = exp_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out_path = reports_dir / "aggregate.json"
    out_path.write_text(json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")

    # Print summary table
    click.echo(f"Experiment: {experiment.name}")
    click.echo(
        f"\n{'Configuration':<25} {'Score (auto)':<14} {'Cost/Task':<12} {'Score/$':<10}"
    )
    click.echo(f"{'-' * 25} {'-' * 14} {'-' * 12} {'-' * 10}")

    ranked = sorted(
        config_summaries.items(),
        key=lambda x: x[1].get("mean_automated_score") or 0,
        reverse=True,
    )
    for label, s in ranked:
        auto = (
            f"{s['mean_automated_score']:.2f}"
            if s["mean_automated_score"] is not None
            else "--"
        )
        cost = (
            f"${s['mean_cost_per_task']:.2f}"
            if s["mean_cost_per_task"] is not None
            else "--"
        )
        spd = (
            f"{s['score_per_dollar']:.2f}"
            if s["score_per_dollar"] is not None
            else "--"
        )
        click.echo(f"{label:<25} {auto:<14} {cost:<12} {spd:<10}")

    if pairwise:
        click.echo("\nPairwise Comparisons:")
        for p in pairwise:
            click.echo(
                f"  {p['config_a']} vs {p['config_b']}: "
                f"delta={p['mean_delta']:+.3f}  "
                f"(wins: {p['wins_a']}/{p['wins_b']}/{p['ties']} A/B/tie)  "
                f"d={p['cohens_d'] or '--'}"
            )

    click.echo(f"\nFull results: {out_path}")
