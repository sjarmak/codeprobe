"""codeprobe run — execute eval tasks against an agent."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import click

from codeprobe.adapters.protocol import ALLOWED_PERMISSION_MODES, AgentConfig
from codeprobe.analysis.dual import format_dual_suffix
from codeprobe.cli._output_helpers import (
    emit_envelope,
    emit_event,
    resolve_mode,
)
from codeprobe.cli.errors import DiagnosticError, PrescriptiveError
from codeprobe.cli.json_display import JsonLineListener
from codeprobe.config.defaults import (
    resolve_max_cost_usd,
    resolve_timeout,
    use_v07_defaults,
)
from codeprobe.core.checkpoint import CheckpointStore
from codeprobe.core.events import (
    BudgetWarning,
    EventDispatcher,
    RunEvent,
    RunFinished,
    TaskScored,
)
from codeprobe.core.executor import DryRunEstimate, dry_run_estimate, execute_config
from codeprobe.core.experiment import (
    Experiment,
    load_experiment,
    save_config_results,
    save_experiment,
)
from codeprobe.core.registry import resolve
from codeprobe.models.experiment import CompletedTask, ExperimentConfig
from codeprobe.models.suite import Suite
from codeprobe.trace.content_policy import ContentPolicy
from codeprobe.trace.recorder import (
    TraceBudgetExceeded,
    TraceOverflowPolicy,
    TraceRecorder,
)

logger = logging.getLogger(__name__)


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


def _format_task_status(score: float) -> str:
    """Format score as PASS/FAIL for binary or as a numeric score for partial."""
    if score >= 1.0:
        return "PASS"
    if score <= 0.0:
        return "FAIL"
    return f"{score:.2f}"


def _on_task_complete(result: CompletedTask) -> None:
    """Print task result to stdout (legacy callback, kept for backward compat)."""
    status = _format_task_status(result.automated_score)
    click.echo(f"  {result.task_id}: {status} ({result.duration_seconds:.1f}s)")


class PlainTextListener:
    """RunEventListener that prints human-readable output.

    Handles :class:`TaskScored` (PASS/FAIL to stdout),
    :class:`BudgetWarning` (to stderr), and :class:`RunFinished`
    (summary to stdout).
    """

    def on_event(self, event: RunEvent) -> None:
        if isinstance(event, TaskScored):
            status = _format_task_status(event.automated_score)
            dual_suffix = format_dual_suffix(event.scoring_details)
            click.echo(f"  {event.task_id}: {status} ({event.duration_seconds:.1f}s){dual_suffix}")
        elif isinstance(event, BudgetWarning):
            pct = int(event.threshold_pct * 100)
            sys.stderr.write(
                f"Cost warning: ${event.cumulative_cost:.2f} of ${event.budget:.2f} budget used ({pct}%)\n"
            )
            sys.stderr.flush()
        elif isinstance(event, RunFinished):
            click.echo(
                f"  Finished: {event.completed_count}/{event.total_tasks} tasks, "
                f"mean score {event.mean_score:.2f}, "
                f"total cost ${event.total_cost:.2f}"
            )


class NdjsonStdoutListener:
    """RunEventListener that streams ``record_type="event"`` lines to stdout.

    Used when ``codeprobe run`` is invoked in NDJSON mode (non-TTY default
    or ``--json-lines``). Emits one JSON line per :class:`TaskScored` so
    consumers can observe per-task completion without waiting for the
    terminal envelope.
    """

    def on_event(self, event: RunEvent) -> None:
        if isinstance(event, TaskScored):
            emit_event(
                {
                    "event": "task_done",
                    "task_id": event.task_id,
                    "score": event.automated_score,
                    "duration_seconds": event.duration_seconds,
                    "cost_usd": getattr(event, "cost_usd", None),
                }
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
            sd for sd in d.iterdir() if sd.is_dir() and sd.name in allowed and (sd / "instruction.md").exists()
        )
    return sorted(sd for sd in d.iterdir() if sd.is_dir() and (sd / "instruction.md").exists())


def _filter_tasks_by_suite(
    task_dirs: list[Path],
    suite: Suite,
) -> list[Path]:
    """Filter task directories according to suite criteria.

    Loads each task's task.toml (or metadata.json) to check task_type,
    difficulty, and tags against the suite filters.  Tasks that lack a
    loadable metadata file are excluded when any filter is active.
    """
    from codeprobe.loaders import load_task

    has_filters = bool(suite.task_types or suite.difficulties or suite.tags or suite.task_ids)
    if not has_filters:
        return task_dirs

    # Pre-filter by explicit task_ids (directory name match)
    if suite.task_ids:
        allowed_ids = set(suite.task_ids)
        task_dirs = [td for td in task_dirs if td.name in allowed_ids]

    # If only task_ids filter was set, we're done
    if not (suite.task_types or suite.difficulties or suite.tags):
        return task_dirs

    filtered: list[Path] = []
    for td in task_dirs:
        toml_path = td / "task.toml"
        json_path = td / "metadata.json"
        meta_path = toml_path if toml_path.exists() else (json_path if json_path.exists() else None)
        if meta_path is None:
            continue  # no metadata to filter on

        try:
            task = load_task(meta_path)
        except (ValueError, KeyError):
            logger.warning("Skipping %s: failed to load metadata", td.name)
            continue

        if suite.task_types and task.metadata.task_type not in suite.task_types:
            continue
        if suite.difficulties and task.metadata.difficulty not in suite.difficulties:
            continue
        if suite.tags:
            task_tags = set(task.metadata.tags)
            if not task_tags.intersection(suite.tags):
                continue

        filtered.append(td)

    return filtered


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


def show_prompt_and_exit(
    path: str,
    *,
    config: str | None = None,
    agent: str = "claude",
    model: str | None = None,
) -> None:
    """Print the fully-resolved prompt for the first task and exit."""
    from codeprobe.core.executor import load_instruction
    from codeprobe.core.preamble import (
        DefaultPreambleResolver,
        base_prompt,
        compose_instruction,
    )

    exp_dir = Path(config) if config else Path(path)

    try:
        experiment = load_experiment(exp_dir)
    except (FileNotFoundError, ValueError):
        experiment = None
        codeprobe_dir = Path(path) / ".codeprobe"
        if codeprobe_dir.is_dir():
            if (codeprobe_dir / "experiment.json").is_file():
                exp_dir = codeprobe_dir
                experiment = load_experiment(exp_dir)
            else:
                candidates = sorted(
                    d for d in codeprobe_dir.iterdir() if d.is_dir() and (d / "experiment.json").is_file()
                )
                if len(candidates) == 1:
                    exp_dir = candidates[0]
                    experiment = load_experiment(exp_dir)
        if experiment is None:
            raise DiagnosticError(
                code="NO_EXPERIMENT",
                message=(
                    f"No experiment found in {Path(path) / '.codeprobe'}."
                ),
                diagnose_cmd=f"codeprobe init {path}",
                terminal=True,
                next_steps=[("Initialize", f"codeprobe init {path}")],
                detail={"path": str(path)},
            )

    assert experiment is not None  # narrowed above; keep mypy happy

    # Resolve repo root
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

    if not task_dirs:
        raise DiagnosticError(
            code="NO_TASKS",
            message="No tasks found. Run 'codeprobe mine' first.",
            diagnose_cmd=f"codeprobe validate {path} --json",
            terminal=True,
            next_steps=[
                ("Mine tasks", f"codeprobe mine {path} --json"),
                (
                    "Then run",
                    f"codeprobe run {path} --agent claude --json",
                ),
            ],
            detail={"path": str(path), "tasks_dir": str(tasks_dir)},
        )

    first_task = task_dirs[0]
    exp_config: ExperimentConfig | None = (
        experiment.configs[0] if experiment.configs else None
    )

    instruction_variant = exp_config.instruction_variant if exp_config else None
    preamble_names = exp_config.preambles if exp_config else ()

    instruction = load_instruction(first_task, variant=instruction_variant)

    if preamble_names:
        resolver = DefaultPreambleResolver(
            task_dir=first_task,
            project_dir=repo_root,
            user_dir=Path.home(),
        )
        prompt, _ = compose_instruction(
            instruction,
            repo_root,
            preamble_names=list(preamble_names),
            resolver=resolver,
            task_id=first_task.name,
        )
    else:
        prompt = base_prompt(instruction, repo_root)

    click.echo(prompt)


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
    timeout: int | None = None,
    suite_path: str | None = None,
    trace_overflow: str = "fail",
    trace_deny: tuple[str, ...] = (),
    offline: bool = False,
    offline_expected_run_duration: str = "1h",
    tenant: str | None = None,
    tenant_source: str | None = None,
    json_flag: bool = False,
    no_json_flag: bool = False,
    json_lines_flag: bool = False,
) -> None:
    """Run eval tasks against an AI coding agent.

    When *offline* is True, the credential-TTL preflight from
    ``codeprobe check-infra offline`` is invoked BEFORE any adapter is
    resolved, and ``CODEPROBE_OFFLINE=1`` is exported so subprocesses
    can short-circuit network calls (subsystems currently opt in — see
    ``codeprobe.net.is_offline_mode``).
    """
    from codeprobe.tenant_lock import acquire_tenant_lock

    # R4 tenant lock: serialize concurrent run invocations within the
    # same tenant. See codeprobe.tenant_lock for details.
    _lock_cm = acquire_tenant_lock(tenant or "local", "run")
    _lock_cm.__enter__()
    try:
        # Validate trace_overflow early so programmatic callers get a ValueError
        # before any IO (experiment.json load, adapter resolution, etc.). This
        # keeps the library-level error contract intact even when the CLI layer
        # already constrains the surface via click.Choice.
        if trace_overflow not in ("fail", "truncate"):
            raise ValueError(
                f"trace_overflow must be 'fail' or 'truncate', got {trace_overflow!r}"
            )

        out_mode = resolve_mode(
            "run", json_flag, no_json_flag, json_lines_flag,
        )
        _results_by_config: dict[str, list[CompletedTask]] = {}
        if offline:
            # Fail-loud: the preflight raises click.ClickException on any
            # backend failure. We let it propagate so the adapter is never
            # resolved and no tasks are dispatched.
            from codeprobe.cli.check_infra import run_offline_preflight

            run_offline_preflight(
                offline_expected_run_duration,
                backend_filter=(),
                echo=not quiet,
            )
            # Set the env var for subprocesses AFTER preflight succeeds so a
            # failed preflight leaves the environment untouched.
            os.environ["CODEPROBE_OFFLINE"] = "1"
            # One-line stderr notice so users & agents can see the gate is
            # now armed; ``codeprobe.net.guard_offline`` will raise
            # ``OFFLINE_NET_ATTEMPT`` if a downstream subsystem tries to
            # reach the network.
            if not quiet:
                click.echo(
                    "offline mode: CODEPROBE_OFFLINE=1 set; "
                    "network-touching subsystems will fail loud "
                    "(codeprobe.net.guard_offline active)",
                    err=True,
                )

        # v0.7 gate-on-context defaults — fire only when the env flag is
        # set AND the caller didn't pass an explicit value. v0.6 (unset)
        # keeps the classic Click-default behavior untouched.
        if use_v07_defaults():
            if max_cost_usd is None:
                max_cost_usd, _ = resolve_max_cost_usd()
            if timeout is None:
                # No goal available at this layer; fall back to the quality
                # default (600s). Users running MCP suites pass --timeout.
                timeout, _ = resolve_timeout("quality")

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
                # First: check if experiment.json lives directly in .codeprobe/
                # (created by `codeprobe experiment init --non-interactive`)
                if (codeprobe_dir / "experiment.json").is_file():
                    exp_dir = codeprobe_dir
                    experiment = load_experiment(exp_dir)
                else:
                    # Fallback: look for named experiment subdirectories
                    candidates = sorted(
                        d for d in codeprobe_dir.iterdir() if d.is_dir() and (d / "experiment.json").is_file()
                    )
                    if len(candidates) == 1:
                        exp_dir = candidates[0]
                        experiment = load_experiment(exp_dir)
                    elif len(candidates) > 1:
                        first_candidate = str(candidates[0])
                        raise PrescriptiveError(
                            code="AMBIGUOUS_EXPERIMENT",
                            message=(
                                "Multiple experiments found in "
                                f"{codeprobe_dir}: "
                                + ", ".join(c.name for c in candidates)
                                + ". Use --config to specify which experiment."
                            ),
                            next_try_flag="--config",
                            next_try_value=first_candidate,
                            detail={
                                "candidates": [str(c) for c in candidates],
                            },
                        )
            if experiment is None:
                raise DiagnosticError(
                    code="NO_EXPERIMENT",
                    message=(
                        f"No experiment found in {Path(path) / '.codeprobe'}. "
                        "Run 'codeprobe init <path>' first to set up an experiment."
                    ),
                    diagnose_cmd=f"codeprobe init {path}",
                    terminal=True,
                    next_steps=[("Initialize", f"codeprobe init {path}")],
                    detail={"path": str(path)},
                )

        assert experiment is not None  # narrowed above; keep mypy happy

        try:
            resolve(agent)
        except KeyError as exc:
            raise PrescriptiveError(
                code="UNKNOWN_BACKEND",
                message=f"Unknown agent backend: {exc}",
                next_try_flag="--agent",
                next_try_value="claude",
                detail={"requested": agent},
            ) from exc

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
            checked = [str(tasks_dir)]
            if repo_tasks != tasks_dir:
                checked.append(str(repo_tasks))
            raise DiagnosticError(
                code="NO_TASKS",
                message=(
                    "No tasks found. Run 'codeprobe mine' first. "
                    f"Checked: {', '.join(checked)}"
                ),
                diagnose_cmd=f"codeprobe validate {path} --json",
                terminal=True,
                next_steps=[
                    ("Mine tasks", f"codeprobe mine {path} --json"),
                    (
                        "Then run",
                        f"codeprobe run {path} --agent claude --json",
                    ),
                ],
                detail={"path": str(path), "checked_dirs": checked},
            )

        # Apply suite filtering when a suite.toml path is provided
        if suite_path is not None:
            from codeprobe.loaders.suite import load_suite

            suite = load_suite(Path(suite_path))
            pre_count = len(task_dirs)
            task_dirs = _filter_tasks_by_suite(task_dirs, suite)
            if not task_dirs:
                raise DiagnosticError(
                    code="NO_SUITE_MATCH",
                    message=(
                        f"Suite '{suite.name}' matched 0 of {pre_count} tasks. "
                        "Check suite.toml filters."
                    ),
                    diagnose_cmd=f"codeprobe run --dry-run {path}",
                    terminal=True,
                    detail={
                        "suite_name": suite.name,
                        "suite_path": str(suite_path),
                        "pre_count": pre_count,
                    },
                )
            click.echo(f"Suite '{suite.name}': {len(task_dirs)}/{pre_count} tasks selected")

        configs_to_run = experiment.configs
        if not configs_to_run:
            configs_to_run = [
                ExperimentConfig(label="default", agent=agent, model=model),
            ]
            # Persist the auto-created config so interpret can find it later
            experiment = Experiment(
                name=experiment.name,
                description=experiment.description,
                tasks_dir=experiment.tasks_dir,
                configs=configs_to_run,
                task_ids=experiment.task_ids,
            )
            save_experiment(exp_dir, experiment)

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

        # Pre-create a shared Rich listener when running multiple configs in
        # parallel so a single Live context owns the terminal.
        shared_rich_listener: RichLiveListener | None = None
        if parallel > 1 and len(configs_to_run) > 1 and not quiet and log_format != "json":
            use_rich = force_rich or (_should_use_rich() and not force_plain)
            if use_rich:
                from codeprobe.cli.rich_display import RichLiveListener

                shared_rich_listener = RichLiveListener()

        # R5: one TraceRecorder per experiment writes to <exp_dir>/runs/trace.db.
        # All configs share the DB — event rows are keyed by (run_id, config,
        # task_id, event_seq) so per-config slicing is cheap at query time.
        #
        # ``trace_overflow`` is validated at the top of ``run_eval`` so
        # library callers get a clean ValueError without triggering any
        # experiment-loading side effects.
        overflow_policy = (
            TraceOverflowPolicy.FAIL
            if trace_overflow == "fail"
            else TraceOverflowPolicy.TRUNCATE
        )
        trace_runs_dir = exp_dir / "runs"
        trace_runs_dir.mkdir(parents=True, exist_ok=True)
        trace_db_path = trace_runs_dir / "trace.db"
        trace_content_policy = ContentPolicy(deny_globs=tuple(trace_deny))
        trace_recorder = TraceRecorder(
            trace_db_path,
            run_id=experiment.name,
            overflow=overflow_policy,
            content_policy=trace_content_policy,
        )

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
                raise PrescriptiveError(
                    code="INVALID_PERMISSION_MODE",
                    message=(
                        f"Invalid permission_mode {perm!r} in config "
                        f"{exp_config.label!r}. "
                        f"Allowed: {', '.join(sorted(ALLOWED_PERMISSION_MODES))}"
                    ),
                    next_try_flag="--permission-mode",
                    next_try_value="default",
                    detail={
                        "config_label": exp_config.label,
                        "allowed": sorted(ALLOWED_PERMISSION_MODES),
                    },
                )

            config_adapter = resolve(exp_config.agent or agent)

            # Layered config resolution: defaults < experiment.json < CLI flags
            resolved_model = model if model is not None else exp_config.model
            resolved_timeout = timeout if timeout is not None else exp_config.extra.get("timeout_seconds", 3600)

            logger.debug(
                "Config resolution: model=%s (%s), timeout=%ds (%s)",
                resolved_model,
                "CLI override" if model is not None else "experiment.json",
                resolved_timeout,
                "CLI override" if timeout is not None else "experiment.json",
            )

            agent_config = AgentConfig(
                model=resolved_model,
                permission_mode=perm,
                timeout_seconds=resolved_timeout,
                mcp_config=exp_config.mcp_config,
                allowed_tools=exp_config.allowed_tools,
                disallowed_tools=exp_config.disallowed_tools,
                cwd=str(repo_root),
            )

            issues = config_adapter.preflight(agent_config)
            if issues:
                for issue in issues:
                    click.echo(f"  [{exp_config.label}] Warning: {issue}", err=True)

            # Adapter-specific parallel-mode pre-check (e.g. Claude needs either
            # file creds or an env-var auth token to isolate per-slot state).
            parallel_warn = getattr(config_adapter, "check_parallel_auth", None)
            if callable(parallel_warn):
                msg = parallel_warn(parallel)
                if msg:
                    click.echo(f"  [{exp_config.label}] Warning: {msg}", err=True)

            config_runs_dir = exp_dir / "runs" / exp_config.label
            config_runs_dir.mkdir(parents=True, exist_ok=True)
            legacy_jsonl = config_runs_dir / "checkpoint.jsonl"
            checkpoint_db = config_runs_dir / "checkpoint.db"
            checkpoint_store = CheckpointStore.from_legacy_path(
                legacy_jsonl,
                checkpoint_db,
                config_name=exp_config.label,
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
            if out_mode.mode == "ndjson":
                # NDJSON mode: stream one ``record_type="event"`` per task to
                # stdout. The JsonLineListener (stderr event stream) is still
                # wired when log_format=='json' so CI pipelines see both.
                dispatcher.register(NdjsonStdoutListener())
                if log_format == "json":
                    dispatcher.register(JsonLineListener())
            elif out_mode.mode == "single_envelope":
                # Envelope mode suppresses per-task chatter on stdout; the
                # stderr JsonLineListener remains available when requested.
                if log_format == "json":
                    dispatcher.register(JsonLineListener())
            elif shared_rich_listener is not None:
                dispatcher.register(shared_rich_listener)
            elif log_format == "json":
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
                # Preambles in ExperimentConfig require a resolver to compose
                # into the prompt. Wire up the default layered resolver so the
                # agent actually sees the preamble content (e.g. the
                # Sourcegraph MCP instructions).
                preamble_resolver = None
                if exp_config.preambles:
                    from codeprobe.core.preamble import DefaultPreambleResolver

                    preamble_resolver = DefaultPreambleResolver(
                        task_dir=task_dirs[0] if task_dirs else repo_root,
                        project_dir=repo_root,
                        user_dir=Path.home(),
                    )

                # R6: persist resolved instruction per task before adapter runs.
                # Write is fail-loud (INV1) — any OSError aborts the run.
                from codeprobe.core.executor import load_instruction
                from codeprobe.core.preamble import base_prompt, compose_instruction

                for _td in task_dirs:
                    _instr = load_instruction(_td, variant=exp_config.instruction_variant)
                    if exp_config.preambles and preamble_resolver is not None:
                        _prompt, _ = compose_instruction(
                            _instr,
                            repo_root,
                            preamble_names=list(exp_config.preambles),
                            resolver=preamble_resolver,
                            task_id=_td.name,
                        )
                    else:
                        _prompt = base_prompt(_instr, repo_root)
                    _out = config_runs_dir / _td.name / "instruction.resolved.md"
                    _out.parent.mkdir(parents=True, exist_ok=True)
                    _out.write_text(_prompt, encoding="utf-8")

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
                    preamble_resolver=preamble_resolver,
                    trace_recorder=trace_recorder,
                )
            except KeyboardInterrupt:
                interrupted = True
                results = []
            finally:
                dispatcher.shutdown()

            if interrupted:
                partial = checkpoint_store.load_ids()
                if owns_sandbox:
                    _release_sandbox()
                raise DiagnosticError(
                    code="INTERRUPTED",
                    message=(
                        f"Interrupted — partial results saved "
                        f"({len(partial)} tasks completed)"
                    ),
                    diagnose_cmd=f"codeprobe run {path} --resume",
                    terminal=True,
                    exit_code=130,
                    detail={
                        "partial_task_count": len(partial),
                        "config_label": exp_config.label,
                    },
                )

            if owns_sandbox:
                _release_sandbox()

            save_config_results(exp_dir, exp_config.label, results)

            scores = [r.automated_score for r in results]
            mean = sum(scores) / len(scores) if scores else 0.0
            perfect = sum(1 for s in scores if s >= 1.0)
            scoring = sum(1 for s in scores if s > 0.0)
            if out_mode.mode == "pretty":
                if perfect == scoring:
                    # Binary results — show pass count
                    click.echo(f"  {exp_config.label}: {perfect}/{len(results)} passed")
                else:
                    # Partial scoring — show mean and breakdown
                    click.echo(
                        f"  {exp_config.label}: mean={mean:.2f}, "
                        f"{perfect} perfect + {scoring - perfect} partial / {len(results)}"
                    )
            _results_by_config[exp_config.label] = list(results)
            return exp_config.label, results

        # Run configs in parallel (each config gets its own adapter + checkpoint)
        budget_error: TraceBudgetExceeded | None = None
        try:
            if parallel > 1 and len(configs_to_run) > 1:
                with ThreadPoolExecutor(max_workers=len(configs_to_run)) as pool:
                    futures = {
                        pool.submit(_run_config, c): c.label for c in configs_to_run
                    }
                    for future in as_completed(futures):
                        label = futures[future]
                        try:
                            future.result()
                        except TraceBudgetExceeded as exc:
                            budget_error = exc
                            click.echo(f"  {label}: ERROR — {exc}", err=True)
                        except Exception as exc:
                            click.echo(f"  {label}: ERROR — {exc}", err=True)
            else:
                for exp_config in configs_to_run:
                    try:
                        _run_config(exp_config)
                    except TraceBudgetExceeded as exc:
                        budget_error = exc
                        click.echo(f"  {exp_config.label}: ERROR — {exc}", err=True)
                        break
        finally:
            # Flush pending rows + close the DB connection deterministically.
            try:
                trace_recorder.close()
            except Exception:  # noqa: BLE001 — close must not mask run errors
                logger.exception("Failed to close TraceRecorder cleanly")

        if budget_error is not None:
            # AC6: overflow under policy=fail surfaces via TRACE_BUDGET_EXCEEDED
            # so agents see a structured error; the catalog pins exit_code=2.
            raise PrescriptiveError(
                code="TRACE_BUDGET_EXCEEDED",
                message=f"Trace budget exceeded: {budget_error}",
                next_try_flag="--trace-overflow",
                next_try_value="truncate",
                detail={"error": str(budget_error)},
            )

        if out_mode.mode == "pretty":
            click.echo()
            click.echo("Next: codeprobe interpret .")
            return

        # Envelope / NDJSON terminal summary — PRD §5.3.
        summary_configs = []
        total_tasks = 0
        total_cost = 0.0
        for label, results in _results_by_config.items():
            scores = [r.automated_score for r in results]
            cfg_cost = sum(
                (getattr(r, "cost_usd", 0.0) or 0.0) for r in results
            )
            total_cost += cfg_cost
            total_tasks += len(results)
            summary_configs.append(
                {
                    "label": label,
                    "tasks": len(results),
                    "mean_score": (sum(scores) / len(scores)) if scores else 0.0,
                    "perfect": sum(1 for s in scores if s >= 1.0),
                    "cost_usd": cfg_cost,
                }
            )
        emit_envelope(
            command="run",
            data={
                "experiment": experiment.name,
                "configs": summary_configs,
                "total_tasks": total_tasks,
                "total_cost_usd": total_cost,
                "tenant": tenant,
                "tenant_source": tenant_source,
            },
        )
    finally:
        _lock_cm.__exit__(None, None, None)
