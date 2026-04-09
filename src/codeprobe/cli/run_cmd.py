"""codeprobe run — execute eval tasks against an agent."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import click

from codeprobe.adapters.protocol import ALLOWED_PERMISSION_MODES, AgentConfig
from codeprobe.core.checkpoint import CheckpointStore
from codeprobe.cli.json_display import JsonLineListener
from codeprobe.core.events import (
    BudgetWarning,
    EventDispatcher,
    RunEvent,
    RunFinished,
    TaskScored,
)
from codeprobe.core.executor import DryRunEstimate, dry_run_estimate, execute_config
from codeprobe.core.experiment import load_experiment, save_config_results
from codeprobe.core.registry import resolve
from codeprobe.models.experiment import CompletedTask, ExperimentConfig


def _should_use_rich() -> bool:
    """Return True when the terminal supports a Rich Live display.

    Returns False in CI environments, non-TTY pipes, and dumb terminals.
    """
    if not sys.stderr.isatty():
        return False
    if os.environ.get("CI") is not None:
        return False
    if os.environ.get("GITHUB_ACTIONS") is not None:
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    return True


def _on_task_complete(result: CompletedTask) -> None:
    """Print task result to stdout (legacy callback, kept for backward compat)."""
    status = "PASS" if result.automated_score >= 1.0 else "FAIL"
    click.echo(f"  {result.task_id}: {status} ({result.duration_seconds:.1f}s)")


class PlainTextListener:
    """RunEventListener that prints human-readable output.

    Handles :class:`TaskScored` (PASS/FAIL to stdout),
    :class:`BudgetWarning` (to stderr), and :class:`RunFinished`
    (summary to stdout).
    """

    def on_event(self, event: RunEvent) -> None:
        if isinstance(event, TaskScored):
            status = "PASS" if event.automated_score >= 1.0 else "FAIL"
            click.echo(f"  {event.task_id}: {status} ({event.duration_seconds:.1f}s)")
        elif isinstance(event, BudgetWarning):
            pct = int(event.threshold_pct * 100)
            sys.stderr.write(
                f"Cost warning: ${event.cumulative_cost:.2f} of "
                f"${event.budget:.2f} budget used ({pct}%)\n"
            )
            sys.stderr.flush()
        elif isinstance(event, RunFinished):
            click.echo(
                f"  Finished: {event.completed_count}/{event.total_tasks} tasks, "
                f"mean score {event.mean_score:.2f}, "
                f"total cost ${event.total_cost:.2f}"
            )


def _find_tasks(d: Path, *, task_ids: tuple[str, ...] = ()) -> list[Path]:
    """Discover task subdirectories with instruction.md.

    When *task_ids* is non-empty, only return tasks whose directory name
    appears in that tuple.  This scopes task discovery to the current
    experiment, preventing tasks from other experiments from leaking in.
    """
    if not d.is_dir():
        return []
    if task_ids:
        allowed = set(task_ids)
        return sorted(
            sd
            for sd in d.iterdir()
            if sd.is_dir() and sd.name in allowed and (sd / "instruction.md").exists()
        )
    return sorted(
        sd for sd in d.iterdir() if sd.is_dir() and (sd / "instruction.md").exists()
    )


def _print_dry_run(estimate: DryRunEstimate) -> None:
    """Pretty-print a DryRunEstimate to stdout."""
    cost_lo, cost_hi = estimate.estimated_cost_range
    click.echo("Dry-run estimate (no agents will be spawned):")
    click.echo(f"  Total tasks:            {estimate.total_tasks}")
    click.echo(f"  Total configs:          {estimate.total_configs}")
    click.echo(f"  Total runs:             {estimate.total_runs}")
    click.echo(f"  Max concurrent workers: {estimate.max_concurrent}")
    click.echo(f"  Estimated worktree disk: ~{estimate.estimated_disk_mb} MB")
    click.echo(f"  Estimated cost range:   ${cost_lo:.2f} - ${cost_hi:.2f}")


_sandbox_lock = threading.Lock()
_sandbox_refcount = 0


def _acquire_sandbox() -> None:
    """Increment sandbox ref-count and set env var (thread-safe)."""
    global _sandbox_refcount  # noqa: PLW0603
    with _sandbox_lock:
        _sandbox_refcount += 1
        os.environ["CODEPROBE_SANDBOX"] = "1"


def _release_sandbox() -> None:
    """Decrement sandbox ref-count; clear env var when last owner exits."""
    global _sandbox_refcount  # noqa: PLW0603
    with _sandbox_lock:
        _sandbox_refcount = max(0, _sandbox_refcount - 1)
        if _sandbox_refcount == 0:
            os.environ.pop("CODEPROBE_SANDBOX", None)


def run_eval(
    path: str,
    agent: str = "claude",
    model: str | None = None,
    config: str | None = None,
    max_cost_usd: float | None = None,
    parallel: int = 1,
    repeats: int = 1,
    dry_run: bool = False,
    log_format: str = "text",
    quiet: bool = False,
    force_plain: bool = False,
    force_rich: bool = False,
) -> None:
    """Run eval tasks against an AI coding agent."""
    exp_dir = Path(config) if config else Path(path)

    # Deprecation warning for legacy .evalrc.yaml
    evalrc_path = Path(path) / ".evalrc.yaml"
    if evalrc_path.exists():
        click.echo(
            "Warning: .evalrc.yaml is no longer used. Configuration is in "
            "experiment.json. This file can be safely deleted.",
            err=True,
        )

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError):
        # Try discovering experiment inside .codeprobe/
        experiment = None
        codeprobe_dir = Path(path) / ".codeprobe"
        if codeprobe_dir.is_dir():
            candidates = sorted(
                d
                for d in codeprobe_dir.iterdir()
                if d.is_dir() and (d / "experiment.json").is_file()
            )
            if len(candidates) == 1:
                exp_dir = candidates[0]
                experiment = load_experiment(exp_dir)
            elif len(candidates) > 1:
                click.echo("Multiple experiments found:", err=True)
                for c in candidates:
                    click.echo(f"  {c.name}", err=True)
                click.echo(
                    "Use --config to specify: codeprobe run <path> --config <path>/.codeprobe/<name>",
                    err=True,
                )
                raise SystemExit(1)
        if experiment is None:
            click.echo(
                f"Error: No experiment found in {Path(path) / '.codeprobe'}",
                err=True,
            )
            click.echo("Run 'codeprobe init <path>' first to set up an experiment.")
            raise SystemExit(1)

    try:
        adapter = resolve(agent)
    except KeyError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    # Resolve to the git repo root — `path` may be an experiment subdir.
    try:
        repo_root = Path(
            subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=Path(path).resolve(),
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
        )
    except (subprocess.CalledProcessError, OSError):
        repo_root = Path(path).resolve()

    tasks_dir = exp_dir / experiment.tasks_dir
    repo_tasks = repo_root / ".codeprobe" / experiment.tasks_dir

    task_dirs = _find_tasks(tasks_dir, task_ids=experiment.task_ids)
    if not task_dirs and repo_tasks != tasks_dir:
        task_dirs = _find_tasks(repo_tasks, task_ids=experiment.task_ids)
        if task_dirs:
            tasks_dir = repo_tasks

    if not task_dirs:
        click.echo("No tasks found. Run 'codeprobe mine' first.", err=True)
        click.echo(f"  Checked: {tasks_dir}", err=True)
        if repo_tasks != tasks_dir:
            click.echo(f"  Checked: {repo_tasks}", err=True)
        raise SystemExit(1)

    configs_to_run = experiment.configs or [
        ExperimentConfig(label="default", agent=agent, model=model),
    ]

    if dry_run:
        estimate = dry_run_estimate(
            task_count=len(task_dirs),
            configs_count=len(configs_to_run),
            repeats=repeats,
            parallel=parallel,
            repo_path=repo_root,
        )
        _print_dry_run(estimate)
        return

    def _run_config(exp_config: ExperimentConfig) -> tuple[str, list[CompletedTask]]:
        """Run a single config (called from thread pool or sequentially)."""
        perm = exp_config.permission_mode

        # Eval runs need agents to operate autonomously (write files, run
        # commands). When the user hasn't explicitly chosen a permission mode,
        # upgrade to dangerously_skip with CODEPROBE_SANDBOX=1 so the agent
        # can work without interactive approval.  Uses ref-counted
        # acquire/release so parallel config threads don't race on
        # os.environ.
        owns_sandbox = False
        if perm == "default":
            perm = "dangerously_skip"
            _acquire_sandbox()
            owns_sandbox = True

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
            cwd=str(repo_root),
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

        # Compute directories to exclude from git clean between sequential
        # tasks so the experiment dir (untracked) isn't deleted.
        _clean_excludes: tuple[str, ...] = ()
        try:
            rel = exp_dir.resolve().relative_to(repo_root)
            top_dir = str(rel).split("/")[0]
            if top_dir and top_dir != ".":
                _clean_excludes = (top_dir,)
        except ValueError:
            pass  # experiment dir is outside the repo

        dispatcher = EventDispatcher()
        if log_format == "json":
            dispatcher.register(JsonLineListener())
        elif not quiet:
            use_rich = force_rich or (_should_use_rich() and not force_plain)
            if use_rich:
                from codeprobe.cli.rich_display import RichLiveListener

                dispatcher.register(RichLiveListener())
            else:
                dispatcher.register(PlainTextListener())

        interrupted = False
        try:
            results = execute_config(
                adapter=config_adapter,
                task_dirs=task_dirs,
                repo_path=repo_root,
                experiment_config=exp_config,
                agent_config=agent_config,
                checkpoint_store=checkpoint_store,
                runs_dir=config_runs_dir,
                max_cost_usd=max_cost_usd,
                parallel=parallel,
                repeats=repeats,
                clean_excludes=_clean_excludes,
                event_dispatcher=dispatcher,
            )
        except KeyboardInterrupt:
            interrupted = True
            results = []
        finally:
            dispatcher.shutdown()

        if interrupted:
            partial = checkpoint_store.load_ids()
            click.echo(
                f"\nInterrupted — partial results saved "
                f"({len(partial)} tasks completed)",
                err=True,
            )
            if owns_sandbox:
                _release_sandbox()
            raise SystemExit(130)

        if owns_sandbox:
            _release_sandbox()

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
