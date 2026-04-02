"""codeprobe run — execute eval tasks against an agent."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import click

from codeprobe.adapters.protocol import ALLOWED_PERMISSION_MODES, AgentConfig
from codeprobe.core.checkpoint import CheckpointStore
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
    max_cost_usd: float | None = None,
    parallel: int = 1,
    repeats: int = 1,
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

    def _run_config(exp_config: ExperimentConfig) -> tuple[str, list[CompletedTask]]:
        """Run a single config (called from thread pool or sequentially)."""
        perm = exp_config.permission_mode
        if perm not in ALLOWED_PERMISSION_MODES:
            raise SystemExit(
                f"Error: invalid permission_mode {perm!r} in config "
                f"{exp_config.label!r}. Allowed: {', '.join(sorted(ALLOWED_PERMISSION_MODES))}"
            )

        config_adapter = resolve(exp_config.agent or agent)
        timeout = exp_config.extra.get("timeout_seconds", 300)
        agent_config = AgentConfig(
            model=exp_config.model or model,
            permission_mode=perm,
            timeout_seconds=timeout,
            mcp_config=exp_config.mcp_config,
            cwd=str(Path(path).resolve()),
        )

        issues = config_adapter.preflight(agent_config)
        if issues:
            for issue in issues:
                click.echo(f"  [{exp_config.label}] Warning: {issue}", err=True)

        config_runs_dir = exp_dir / "runs" / exp_config.label
        config_runs_dir.mkdir(parents=True, exist_ok=True)
        legacy_jsonl = config_runs_dir / "checkpoint.jsonl"
        checkpoint_db = config_runs_dir / "checkpoint.db"
        checkpoint_store = CheckpointStore.from_legacy_path(
            legacy_jsonl, checkpoint_db, config_name=exp_config.label
        )

        click.echo(f"\nRunning config: {exp_config.label} ({len(task_dirs)} tasks)")

        results = execute_config(
            adapter=config_adapter,
            task_dirs=task_dirs,
            repo_path=Path(path).resolve(),
            experiment_config=exp_config,
            agent_config=agent_config,
            checkpoint_store=checkpoint_store,
            runs_dir=config_runs_dir,
            on_task_complete=_on_task_complete,
            max_cost_usd=max_cost_usd,
            parallel=parallel,
            repeats=repeats,
        )

        save_config_results(exp_dir, exp_config.label, results)

        passed = sum(1 for r in results if r.automated_score >= 1.0)
        click.echo(f"  {exp_config.label}: {passed}/{len(results)} passed")
        return exp_config.label, results

    # Run configs in parallel (each config gets its own adapter + checkpoint)
    if parallel > 1 and len(configs_to_run) > 1:
        with ThreadPoolExecutor(max_workers=len(configs_to_run)) as pool:
            futures = {pool.submit(_run_config, c): c.label for c in configs_to_run}
            for future in as_completed(futures):
                label = futures[future]
                try:
                    future.result()
                except Exception as exc:
                    click.echo(f"  {label}: ERROR — {exc}", err=True)
    else:
        for exp_config in configs_to_run:
            _run_config(exp_config)

    click.echo()
    click.echo("Next: codeprobe interpret .")
