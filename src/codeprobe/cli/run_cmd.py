"""codeprobe run — execute eval tasks against an agent."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import click

from codeprobe.adapters.models import validate_model
from codeprobe.adapters.protocol import (
    ALLOWED_PERMISSION_MODES,
    AgentConfig,
    quarantine_message,
)
from codeprobe.analysis.dual import format_dual_suffix
from codeprobe.analysis.stats import partition_reward_population
from codeprobe.analysis.validity import is_infra_failure
from codeprobe.cli._output_helpers import (
    emit_envelope,
    emit_event,
    resolve_mode,
)
from codeprobe.cli.capability_preflight import check_arm_capabilities
from codeprobe.cli.errors import DiagnosticError, PrescriptiveError
from codeprobe.cli.json_display import JsonLineListener
from codeprobe.config.defaults import (
    resolve_max_cost_usd,
    resolve_timeout,
    use_v07_defaults,
)
from codeprobe.core.checkpoint import CheckpointStore
from codeprobe.core.containment import (
    DISCLOSURE,
    resolve_containment,
    set_active_plan,
)
from codeprobe.core.events import (
    BudgetWarning,
    EventDispatcher,
    RunEvent,
    RunFinished,
    TaskScored,
)
from codeprobe.core.executor import (
    DryRunEstimate,
    dry_run_estimate,
    execute_config,
    resolve_instruction_variant,
)
from codeprobe.core.experiment import (
    Experiment,
    load_experiment,
    save_config_results,
    save_experiment,
)
from codeprobe.core.isolation import _discover_experiment_dirs
from codeprobe.core.mcp_policy import resolve_tool_policy
from codeprobe.core.registry import resolve
from codeprobe.models.experiment import CompletedTask, ExperimentConfig
from codeprobe.models.suite import Suite
from codeprobe.trace.content_policy import ContentPolicy
from codeprobe.trace.recorder import (
    TraceBudgetExceededError,
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


def _is_codeprobe_owned(rel_path: str, experiment_dir_names: frozenset[str]) -> bool:
    """Return True when a porcelain status path is a codeprobe-owned artifact.

    Codeprobe writes into ``.codeprobe/``, ``.codeprobe-worktrees*``,
    ``runs/``, and top-level experiment directories (any directory holding
    an ``experiment.json``). Untracked/modified entries under those paths
    must never trigger the dirty-checkout refusal.
    """
    top = rel_path.split("/", 1)[0]
    if top in (".codeprobe", "runs"):
        return True
    if top.startswith(".codeprobe-worktrees"):
        return True
    return top in experiment_dir_names


def assert_clean_checkout(repo_root: Path, *, allow_dirty: bool = False) -> None:
    """Hard-refuse ``codeprobe run`` on a dirty checkout (codeprobe-f7rl.1).

    Trial worktrees are created detached from HEAD
    (``codeprobe.core.isolation``), so any uncommitted change in *repo_root*
    is invisible to every trial — the eval would measure a tree the customer
    isn't looking at.

    Raises
    ------
    DiagnosticError(NOT_A_GIT_REPO)
        When *repo_root* is not a git work tree (or git cannot inspect it):
        worktree isolation cannot function at all.
    PrescriptiveError(DIRTY_CHECKOUT)
        When the checkout has a tracked modification, staged change, or
        untracked file outside codeprobe-owned dirs and *allow_dirty* is
        False. With *allow_dirty* True, a one-line stderr disclosure is
        emitted instead and the run proceeds.
    """
    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=normal"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
        stderr = getattr(exc, "stderr", "") or ""
        raise DiagnosticError(
            code="NOT_A_GIT_REPO",
            message=(
                f"{repo_root} is not a git work tree (or git cannot inspect "
                "it), so worktree isolation cannot function. Run codeprobe "
                "from inside a git repository: clone the repo, or git init "
                "and commit first."
            ),
            diagnose_cmd=f"git -C {repo_root} rev-parse --is-inside-work-tree",
            detail={"repo_root": str(repo_root), "git_stderr": stderr.strip()},
        ) from exc

    experiment_dir_names = frozenset(_discover_experiment_dirs(repo_root))
    offending: list[str] = []
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        rel = line[3:]
        if " -> " in rel:  # rename entry: "XY orig -> dest"
            rel = rel.split(" -> ", 1)[1]
        rel = rel.strip().strip('"')
        if not _is_codeprobe_owned(rel, experiment_dir_names):
            offending.append(rel)

    if not offending:
        return

    if allow_dirty:
        click.echo(
            f"--allow-dirty: {len(offending)} uncommitted change(s) in "
            f"{repo_root} will NOT be visible to agents (worktrees are "
            "created from HEAD).",
            err=True,
        )
        return

    shown = offending[:10]
    remainder = len(offending) - len(shown)
    listing = "\n  ".join(shown)
    if remainder:
        listing += f"\n  (and {remainder} more)"
    raise PrescriptiveError(
        code="DIRTY_CHECKOUT",
        message=(
            f"Refusing to run: {repo_root} has uncommitted changes:\n"
            f"  {listing}\n"
            "Worktrees are created from HEAD, so uncommitted changes are "
            "excluded from every trial. Commit or stash first, or pass "
            "--allow-dirty to run against HEAD anyway."
        ),
        next_try_flag="--allow-dirty",
        next_try_value="",
        detail={
            "repo_root": str(repo_root),
            "dirty_paths": shown,
            "dirty_count": len(offending),
        },
    )


def _format_task_status(score: float) -> str:
    """Format score as PASS/FAIL for binary or as a numeric score for partial."""
    if score >= 1.0:
        return "PASS"
    if score <= 0.0:
        return "FAIL"
    return f"{score:.2f}"


def build_run_envelope_summary(
    results_by_config: dict[str, list[CompletedTask]],
) -> tuple[list[dict], int, float]:
    """Shape per-config rows for the run envelope / NDJSON terminal summary.

    The headline ``mean_score`` and ``perfect`` count are over the scorable
    reward population only (non-executed status=="error" runs — quota
    casualties, invalid-model/crash errors — excluded, see
    partition_reward_population); ``tasks`` and ``cost_usd`` stay over all
    attempts since errored trials are real, billed work, and
    ``quota_error_count`` / ``infra_failure_count`` / ``errored_count`` surface
    how many were excluded (codeprobe-9jxx; codeprobe-h3j4; codeprobe-77z).

    The three exclusion counts nest: ``quota_error_count`` (OAuth/API limit)
    ⊆ ``infra_failure_count`` (every infrastructure casualty) ⊆
    ``errored_count`` (everything dropped from the reward population). Reporting
    the infra subset here — not only in ``codeprobe interpret`` — is what keeps
    the widened exclusion honest on the surface users actually read: without it,
    a crash-inflated mean would look identical to a clean one.

    Returns ``(summary_configs, total_tasks, total_cost)``.
    """
    summary_configs: list[dict] = []
    total_tasks = 0
    total_cost = 0.0
    for label, results in results_by_config.items():
        reward_results, quota_errors, errored = partition_reward_population(results)
        infra_failures = sum(1 for r in results if is_infra_failure(r))
        scores = [r.automated_score for r in reward_results]
        cfg_cost = sum((getattr(r, "cost_usd", 0.0) or 0.0) for r in results)
        total_cost += cfg_cost
        total_tasks += len(results)
        summary_configs.append(
            {
                "label": label,
                "tasks": len(results),
                "quota_error_count": quota_errors,
                "infra_failure_count": infra_failures,
                "errored_count": errored,
                "scored_count": len(reward_results),
                "mean_score": (sum(scores) / len(scores)) if scores else 0.0,
                "perfect": sum(1 for s in scores if s >= 1.0),
                "cost_usd": cfg_cost,
            }
        )
    return summary_configs, total_tasks, total_cost


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


def _check_ground_truth_present(task_dirs: list[Path], path: str) -> None:
    """Reject artifact_eval/dual tasks whose ground_truth.json oracle is missing.

    Loads each task's task.toml (or metadata.json) to read
    ``verification.verification_mode``. Tasks in "artifact_eval" or "dual"
    mode are scored by ``ArtifactScorer``, which looks up
    ``tests/ground_truth.json`` and falls back to the legacy task-root
    ``ground_truth.json`` (mirrors the lookup in
    ``core/scoring/scorers.py:ArtifactScorer.score``). A stale or interrupted
    ``codeprobe mine`` run can leave a task in one of those modes with
    neither file present; without this check it scores ``verifier_error`` on
    every trial instead of failing loudly here.
    """
    from codeprobe.loaders import load_task

    missing: list[str] = []
    for td in task_dirs:
        toml_path = td / "task.toml"
        json_path = td / "metadata.json"
        meta_path = toml_path if toml_path.exists() else (json_path if json_path.exists() else None)
        if meta_path is None:
            continue  # no metadata to check

        try:
            task = load_task(meta_path)
        except (ValueError, KeyError):
            logger.warning(
                "Skipping ground-truth preflight for %s: failed to load metadata", td.name
            )
            continue

        if task.verification.verification_mode not in ("artifact_eval", "dual"):
            continue

        gt_path = td / "tests" / "ground_truth.json"
        if not gt_path.exists():
            gt_path = td / "ground_truth.json"
        if not gt_path.exists():
            missing.append(td.name)

    if not missing:
        return

    raise DiagnosticError(
        code="MISSING_GROUND_TRUTH",
        message=(
            f"{len(missing)} artifact_eval/dual task(s) missing "
            f"tests/ground_truth.json: {', '.join(missing)}. These would "
            "score verifier_error instead of a real result."
        ),
        diagnose_cmd=f"codeprobe validate {path} --json",
        terminal=True,
        next_steps=[
            (
                "Re-mine to regenerate ground truth",
                f"codeprobe mine {path} --dual-verify",
            ),
        ],
        detail={"path": path, "missing_ground_truth_tasks": missing},
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

    instruction_variant = (
        resolve_instruction_variant(
            first_task,
            variant=exp_config.instruction_variant,
            mcp_config=exp_config.mcp_config,
        )
        if exp_config
        else None
    )
    preamble_names = exp_config.preambles if exp_config else ()

    instruction = load_instruction(first_task, variant=instruction_variant)

    if preamble_names:
        from codeprobe.core.preamble import task_preamble_context
        from codeprobe.core.scoring import read_task_metadata

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
            extra_context=task_preamble_context(
                read_task_metadata(first_task),
                preamble_names=preamble_names,
                task_id=first_task.name,
            )
            or None,
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
    config_parallel: int = 1,
    repeats: int = 1,
    dry_run: bool = False,
    allow_dirty: bool = False,
    uncontained: bool = False,
    log_format: str = "text",
    quiet: bool = False,
    force_plain: bool = False,
    force_rich: bool = False,
    timeout: int | None = None,
    max_turns: int | None = None,
    suite_path: str | None = None,
    trace_overflow: str = "fail",
    trace_deny: tuple[str, ...] = (),
    pristine_config: bool = False,
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

        # Dirty-checkout preflight (codeprobe-f7rl.1): worktrees detach from
        # HEAD, so uncommitted work is invisible to every trial. Refuse hard
        # before any adapter is resolved; --dry-run only estimates, so it is
        # exempt.
        if not dry_run:
            assert_clean_checkout(repo_root, allow_dirty=allow_dirty)

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

        # Validate every config's model token up front — before task discovery
        # and before any agent is spawned. An unknown token must fail loudly
        # here with a prescriptive error; otherwise it flows through to the
        # agent CLI, errors deep in the run, and is scored 0.0, silently
        # corrupting the comparison (codeprobe-fvfo Gap 1/2). Layered
        # resolution mirrors _run_config: a CLI --model override wins, else
        # the config's own model.
        _configs_to_validate = experiment.configs or [
            ExperimentConfig(label="default", agent=agent, model=model)
        ]
        for cfg in _configs_to_validate:
            validate_model(
                cfg.agent or agent,
                model if model is not None else cfg.model,
            )

        # Refuse, pre-spend, any arm whose knobs its adapter cannot honor
        # (codeprobe-f7rl.26). Fail-closed: an adapter that declares no
        # capabilities is treated as prompt+model only. Without this gate
        # the knobs silently no-op (e.g. copilot never blocks
        # Grep/Bash/Glob/Read under mcp_mode=strict) and the report
        # compares arms that never differed. Resolving here also surfaces
        # a typo'd per-arm agent before the other arm spends money.
        for cfg in _configs_to_validate:
            try:
                arm_adapter = resolve(cfg.agent or agent)
            except KeyError as exc:
                raise PrescriptiveError(
                    code="UNKNOWN_BACKEND",
                    message=(
                        f"Unknown agent backend in config {cfg.label!r}: {exc}"
                    ),
                    next_try_flag="--agent",
                    next_try_value="claude",
                    detail={
                        "config_label": cfg.label,
                        "requested": cfg.agent or agent,
                    },
                ) from exc
            # Quarantined adapters (codeprobe-f7rl decision 4, currently
            # codex) are registered — so the failure is prescriptive, not
            # a raw KeyError — but must never dispatch: their all-zero
            # arms would enter means as valid measurements. Refuse the
            # whole run upfront, before any arm spends.
            if getattr(arm_adapter, "quarantined", False):
                arm_agent = cfg.agent or agent
                raise PrescriptiveError(
                    code="ADAPTER_QUARANTINED",
                    message=quarantine_message(arm_agent),
                    terminal=True,
                    next_try_flag="--agent",
                    next_try_value="claude",
                    detail={
                        "config_label": cfg.label,
                        "adapter": getattr(
                            arm_adapter, "name", type(arm_adapter).__name__
                        ),
                        "requested": arm_agent,
                    },
                )
            check_arm_capabilities(cfg, arm_adapter, cli_max_turns=max_turns)

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

        # Fail loud on a stale/interrupted mine run before any adapter spawns
        # (codeprobe-yxex) — applies whether or not a suite filter was given.
        _check_ground_truth_present(task_dirs, path)

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

        # Containment gate (codeprobe-f7rl.3): a real run launches an
        # autonomous agent with --dangerously-skip-permissions plus mined
        # third-party test/verifier scripts. Outside a container this needs
        # explicit --uncontained consent; refuse hard before any config
        # dispatch. --dry-run only estimates, so it returns above without
        # reaching this gate.
        containment_plan = resolve_containment(uncontained)
        set_active_plan(containment_plan)
        if containment_plan.mode == "host-consented":
            click.echo(f"--uncontained accepted: {DISCLOSURE}", err=True)

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
            # commands). When the user hasn't explicitly chosen a permission
            # mode, upgrade to dangerously_skip. WHERE that is allowed to
            # execute was already decided once per run by the containment
            # gate in ``run_eval`` (codeprobe.core.containment) — codeprobe
            # never sets CODEPROBE_SANDBOX itself; that env var is a
            # user-set consent signal only.
            if perm == "default":
                perm = "dangerously_skip"

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
            # max_turns: explicit field wins, fall back to extra dict for
            # configs authored before the field existed; CLI flag overrides
            # both.
            cfg_max_turns = (
                exp_config.max_turns
                if exp_config.max_turns is not None
                else exp_config.extra.get("max_turns")
            )
            resolved_max_turns = (
                max_turns if max_turns is not None else cfg_max_turns
            )
            # Source of the explicit config-level cap (if any), threaded
            # into the executor so the per-trial turn-cap resolver can
            # label max_turns_source honestly. Empty when no explicit cap
            # is set and the family default / task override decides.
            if max_turns is not None:
                config_max_turns_source = "cli"
            elif cfg_max_turns is not None:
                config_max_turns_source = "experiment"
            else:
                config_max_turns_source = ""

            logger.debug(
                "Config resolution: model=%s (%s), timeout=%ds (%s), max_turns=%s (%s)",
                resolved_model,
                "CLI override" if model is not None else "experiment.json",
                resolved_timeout,
                "CLI override" if timeout is not None else "experiment.json",
                resolved_max_turns,
                "CLI override" if max_turns is not None else "experiment.json",
            )

            policy = resolve_tool_policy(exp_config)
            if policy.warning is not None:
                click.echo(
                    f"  [{exp_config.label}] Warning: {policy.warning}",
                    err=True,
                )
            agent_config = AgentConfig(
                model=resolved_model,
                permission_mode=perm,
                timeout_seconds=resolved_timeout,
                mcp_config=exp_config.mcp_config,
                allowed_tools=policy.allowed_tools,
                disallowed_tools=policy.disallowed_tools,
                cwd=str(repo_root),
                max_turns=resolved_max_turns,
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
                from codeprobe.core.preamble import (
                    base_prompt,
                    compose_instruction,
                    task_preamble_context,
                )
                from codeprobe.core.scoring import read_task_metadata

                for _td in task_dirs:
                    _variant = resolve_instruction_variant(
                        _td,
                        variant=exp_config.instruction_variant,
                        mcp_config=exp_config.mcp_config,
                    )
                    _instr = load_instruction(_td, variant=_variant)
                    if exp_config.preambles and preamble_resolver is not None:
                        _extra_context = task_preamble_context(
                            read_task_metadata(_td),
                            preamble_names=exp_config.preambles,
                            task_id=_td.name,
                        )
                        _prompt, _ = compose_instruction(
                            _instr,
                            repo_root,
                            preamble_names=list(exp_config.preambles),
                            resolver=preamble_resolver,
                            task_id=_td.name,
                            extra_context=_extra_context or None,
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
                    event_dispatcher=dispatcher,
                    preamble_resolver=preamble_resolver,
                    trace_recorder=trace_recorder,
                    config_max_turns_source=config_max_turns_source,
                    pristine_config=pristine_config,
                )
            except KeyboardInterrupt:
                interrupted = True
                results = []
            finally:
                dispatcher.shutdown()

            if interrupted:
                partial = checkpoint_store.load_ids()
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

            save_config_results(exp_dir, exp_config.label, results)

            # Mean and pass-rate exclude non-executed runs (status=="error":
            # quota casualties, invalid-model/crash errors — see
            # partition_reward_population); the excluded count is surfaced so a
            # run where nothing actually executed never reads as "0.00 passed"
            # (codeprobe-h3j4 / codeprobe-9jxx).
            reward_results, quota_errors, errored = partition_reward_population(
                results
            )
            infra_failures = sum(1 for r in results if is_infra_failure(r))
            scores = [r.automated_score for r in reward_results]
            mean = sum(scores) / len(scores) if scores else 0.0
            perfect = sum(1 for s in scores if s >= 1.0)
            scoring = sum(1 for s in scores if s > 0.0)
            if out_mode.mode == "pretty":
                # Name the infra subset in the note. "3 errored, excluded" reads
                # as three bad solutions; "3 errored, excluded (2 infra)" says
                # the run is provisional and those two need a re-run before the
                # mean is quotable (codeprobe-77z).
                infra_note = f", {infra_failures} infra" if infra_failures else ""
                err_note = (
                    f" ({errored} errored, excluded{infra_note})" if errored else ""
                )
                if not reward_results:
                    # No run actually executed — do NOT print a 0/0 "passed"
                    # line that looks like a real (failing) measurement.
                    click.echo(
                        f"  {exp_config.label}: ERRORED — "
                        f"{errored}/{len(results)} runs did not execute "
                        f"(no score)"
                    )
                elif perfect == scoring:
                    # Binary results — show pass count
                    click.echo(
                        f"  {exp_config.label}: "
                        f"{perfect}/{len(reward_results)} passed{err_note}"
                    )
                else:
                    # Partial scoring — show mean and breakdown
                    click.echo(
                        f"  {exp_config.label}: mean={mean:.2f}, "
                        f"{perfect} perfect + {scoring - perfect} partial "
                        f"/ {len(reward_results)}{err_note}"
                    )
            _results_by_config[exp_config.label] = list(results)
            return exp_config.label, results

        # Run configs in parallel only when config_parallel > 1 AND there
        # are multiple configs. Default config_parallel=1 dispatches configs
        # sequentially so --max-cost-usd holds within parallel × per-task-cost
        # of overshoot rather than config_parallel × parallel × per-task-cost
        # (codeprobe-emez fix).
        effective_config_parallel = min(config_parallel, len(configs_to_run))
        budget_error: TraceBudgetExceededError | None = None
        try:
            if effective_config_parallel > 1:
                with ThreadPoolExecutor(
                    max_workers=effective_config_parallel
                ) as pool:
                    futures = {
                        pool.submit(_run_config, c): c.label for c in configs_to_run
                    }
                    for future in as_completed(futures):
                        label = futures[future]
                        try:
                            future.result()
                        except TraceBudgetExceededError as exc:
                            budget_error = exc
                            click.echo(f"  {label}: ERROR — {exc}", err=True)
                        except Exception as exc:
                            click.echo(f"  {label}: ERROR — {exc}", err=True)
            else:
                for exp_config in configs_to_run:
                    try:
                        _run_config(exp_config)
                    except TraceBudgetExceededError as exc:
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
        summary_configs, total_tasks, total_cost = build_run_envelope_summary(
            _results_by_config
        )
        emit_envelope(
            command="run",
            data={
                "experiment": experiment.name,
                "configs": summary_configs,
                "total_tasks": total_tasks,
                "total_cost_usd": total_cost,
                "pristine_config": pristine_config,
                "tenant": tenant,
                "tenant_source": tenant_source,
            },
        )
    finally:
        _lock_cm.__exit__(None, None, None)
