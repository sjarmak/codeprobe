"""codeprobe run — execute eval tasks against an agent."""

from __future__ import annotations

from pathlib import Path

import click

from codeprobe.adapters.protocol import ALLOWED_PERMISSION_MODES, AgentConfig
from codeprobe.core.executor import execute_config
from codeprobe.core.experiment import load_experiment, save_config_results
from codeprobe.core.registry import resolve
from codeprobe.models.experiment import CompletedTask, ExperimentConfig


def _on_task_complete(result: CompletedTask) -> None:
    """Print task result to stdout."""
    status = "PASS" if result.automated_score >= 1.0 else "FAIL"
    click.echo(f"  {result.task_id}: {status} ({result.duration_seconds:.1f}s)")


def run_eval(
    path: str,
    agent: str = "claude",
    model: str | None = None,
    config: str | None = None,
) -> None:
    """Run eval tasks against an AI coding agent."""
    exp_dir = Path(config) if config else Path(path)

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        click.echo("Run 'codeprobe init' first to set up an experiment.")
        raise SystemExit(1)

    try:
        adapter = resolve(agent)
    except KeyError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    tasks_dir = exp_dir / experiment.tasks_dir
    if not tasks_dir.is_dir():
        click.echo(f"Error: Tasks directory not found: {tasks_dir}", err=True)
        raise SystemExit(1)

    task_dirs = sorted(
        d for d in tasks_dir.iterdir() if d.is_dir() and (d / "instruction.md").exists()
    )

    if not task_dirs:
        click.echo("No tasks found. Run 'codeprobe mine' first.", err=True)
        raise SystemExit(1)

    configs_to_run = experiment.configs or [
        ExperimentConfig(label="default", agent=agent, model=model),
    ]

    for exp_config in configs_to_run:
        click.echo(f"\nRunning config: {exp_config.label} ({len(task_dirs)} tasks)")

        perm = exp_config.permission_mode
        if perm not in ALLOWED_PERMISSION_MODES:
            click.echo(
                f"Error: invalid permission_mode {perm!r} in config "
                f"{exp_config.label!r}. Allowed: {', '.join(sorted(ALLOWED_PERMISSION_MODES))}",
                err=True,
            )
            raise SystemExit(1)

        agent_config = AgentConfig(
            model=exp_config.model or model,
            permission_mode=perm,
            timeout_seconds=300,
            mcp_config=exp_config.mcp_config,
        )

        issues = adapter.preflight(agent_config)
        if issues:
            for issue in issues:
                click.echo(f"  Warning: {issue}", err=True)

        checkpoint_path = exp_dir / "runs" / exp_config.label / "checkpoint.jsonl"
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)

        results = execute_config(
            adapter=adapter,
            task_dirs=task_dirs,
            repo_path=Path(path).resolve(),
            experiment_config=exp_config,
            agent_config=agent_config,
            checkpoint_path=checkpoint_path,
            on_task_complete=_on_task_complete,
        )

        save_config_results(exp_dir, exp_config.label, results)

        passed = sum(1 for r in results if r.automated_score >= 1.0)
        click.echo(f"  {exp_config.label}: {passed}/{len(results)} passed")

    click.echo()
    click.echo("Next: codeprobe interpret .")
