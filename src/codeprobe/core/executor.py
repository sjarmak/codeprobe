"""Task execution — run agents on tasks and collect results."""

from __future__ import annotations

import json as _json
import logging
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from codeprobe.core.checkpoint import CheckpointStore
from codeprobe.core.events import (
    BudgetChecker,
    EventDispatcher,
    RunFinished,
    RunStarted,
    TaskScored,
    TaskStarted,
)
from codeprobe.core.isolation import (
    IsolationStrategy,
    WorktreeIsolation,
    git_pin_commit,
    git_restore_clean,
)
from codeprobe.core.preamble import PreambleResolver, _base_prompt, compose_instruction
from codeprobe.core.scoring import get_scorer, sanitize_secrets
from codeprobe.models.experiment import CompletedTask, ExperimentConfig

if TYPE_CHECKING:
    from codeprobe.adapters.protocol import AgentAdapter, AgentConfig


@dataclass(frozen=True)
class DryRunEstimate:
    """Resource estimate for a dry-run (no agents spawned)."""

    total_tasks: int
    total_configs: int
    total_runs: int
    max_concurrent: int
    estimated_disk_mb: float
    estimated_cost_range: tuple[float, float]


def _estimate_repo_size_mb(repo_path: Path) -> float:
    """Estimate the on-disk size of a repo in megabytes.

    Uses ``du -sm`` for speed; falls back to a conservative default.
    """
    try:
        result = subprocess.run(
            ["du", "-sm", str(repo_path)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return float(result.stdout.split()[0])
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        pass
    return 100.0  # conservative default


def dry_run_estimate(
    *,
    task_count: int,
    configs_count: int,
    repeats: int,
    parallel: int,
    repo_path: Path,
) -> DryRunEstimate:
    """Compute resource estimates without spawning any agents.

    Returns a frozen dataclass with counts, concurrency, disk, and cost
    projections.
    """
    total_runs = task_count * configs_count * repeats
    max_concurrent = min(parallel, total_runs)
    repo_mb = _estimate_repo_size_mb(repo_path)
    # Each parallel worker needs its own worktree copy
    estimated_disk_mb = repo_mb * max_concurrent

    # Cost heuristic: $0.02 - $0.15 per run (typical for light coding tasks)
    cost_low = total_runs * 0.02
    cost_high = total_runs * 0.15

    return DryRunEstimate(
        total_tasks=task_count,
        total_configs=configs_count,
        total_runs=total_runs,
        max_concurrent=max_concurrent,
        estimated_disk_mb=round(estimated_disk_mb, 1),
        estimated_cost_range=(round(cost_low, 2), round(cost_high, 2)),
    )


# Global concurrency semaphore — caps total active agent subprocesses
# across all executor instances in the process.
_global_semaphore: threading.Semaphore | None = None
_semaphore_lock = threading.Lock()


def set_max_concurrency(max_concurrent: int) -> None:
    """Set the global concurrency cap for agent subprocesses."""
    global _global_semaphore  # noqa: PLW0603
    with _semaphore_lock:
        _global_semaphore = threading.Semaphore(max_concurrent)


def get_concurrency_semaphore() -> threading.Semaphore | None:
    """Return the global semaphore (None if not configured)."""
    return _global_semaphore


logger = logging.getLogger(__name__)


def _classify_error(exc: BaseException) -> str:
    """Classify an exception into an error category.

    Returns one of: 'timeout', 'system', 'agent'.
    """
    if isinstance(exc, subprocess.TimeoutExpired):
        return "timeout"
    if isinstance(exc, (OSError, MemoryError)):
        return "system"
    return "agent"


@dataclass(frozen=True)
class TaskResult:
    """Completed task plus raw agent output for trace storage."""

    completed: CompletedTask
    agent_stdout: str = ""
    agent_stderr: str = ""


def load_instruction(task_dir: Path, variant: str | None = None) -> str:
    """Read the instruction file from a task directory.

    Falls back to instruction.md if the variant file doesn't exist.
    Raises FileNotFoundError if no instruction file is found.
    """
    if variant:
        variant_path = (task_dir / variant).resolve()
        if not str(variant_path).startswith(str(task_dir.resolve())):
            raise ValueError(f"instruction_variant escapes task directory: {variant!r}")
        if variant_path.is_file():
            return variant_path.read_text(encoding="utf-8").strip()

    default_path = task_dir / "instruction.md"
    if default_path.is_file():
        return default_path.read_text(encoding="utf-8").strip()

    raise FileNotFoundError(f"No instruction file found in {task_dir}")


def execute_task(
    adapter: AgentAdapter,
    task_dir: Path,
    repo_path: Path,
    agent_config: AgentConfig,
    instruction_variant: str | None = None,
    reward_type: str = "binary",
    preamble_names: tuple[str, ...] = (),
    preamble_resolver: PreambleResolver | None = None,
    worktree_path: Path | None = None,
    session_env: dict[str, str] | None = None,
) -> TaskResult:
    """Execute a single task and return a TaskResult with trace data.

    Never raises — errors are captured in the result metadata.
    """
    task_id = task_dir.name

    # Load task metadata once — used for reward_type auto-detection and
    # preamble context (e.g. sg_repo for Sourcegraph preamble).
    _task_meta: dict = {}
    meta_path = task_dir / "metadata.json"
    if meta_path.is_file():
        try:
            import json as _json

            _task_meta = _json.loads(meta_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass

    # Auto-detect reward_type from task metadata when caller uses default.
    # Oracle tasks (org-scale) need "continuous" scoring to read reward.txt;
    # the default "binary" would score exit-code-only and always pass.
    if reward_type == "binary":
        task_rt = (_task_meta.get("verification") or {}).get("reward_type")
        if task_rt and task_rt != "binary":
            reward_type = task_rt

    # Remove stale answer.txt / reward.txt from prior runs so they don't
    # leak into this task's scoring sandbox.
    for stale in ("answer.txt", "reward.txt"):
        stale_path = task_dir / stale
        if stale_path.is_file():
            stale_path.unlink(missing_ok=True)

    def _error_result(error: str, error_category: str | None = None) -> TaskResult:
        return TaskResult(
            completed=CompletedTask(
                task_id=task_id,
                automated_score=0.0,
                status="error",
                error_category=error_category,
                metadata={"error": error},
            ),
        )

    try:
        instruction = load_instruction(task_dir, variant=instruction_variant)
    except FileNotFoundError as exc:
        return _error_result(str(exc))

    resolved_preambles: list[dict[str, str]] = []
    if preamble_names and preamble_resolver is None:
        return _error_result(
            f"preambles={preamble_names!r} requested but no "
            "preamble_resolver provided"
        )

    if preamble_names and preamble_resolver is not None:
        # Build extra context from task metadata for preamble templates
        extra_ctx: dict[str, str] = {}
        sg_repo = (_task_meta.get("metadata") or {}).get("sg_repo", "")
        if sg_repo:
            extra_ctx["sg_repo"] = sg_repo

        try:
            prompt, resolved_preambles = compose_instruction(
                instruction,
                repo_path,
                preamble_names=list(preamble_names),
                resolver=preamble_resolver,
                task_id=task_id,
                worktree_path=worktree_path,
                extra_context=extra_ctx or None,
            )
        except (FileNotFoundError, ValueError) as exc:
            return _error_result(f"Preamble resolution failed: {exc}")
    else:
        prompt = _base_prompt(instruction, repo_path, worktree_path=worktree_path)

    # Pin workspace to pre-merge commit when task has a ground_truth_commit.
    # The agent starts from the parent of the merge commit (the state before
    # the PR landed) and must reproduce the changes.
    pin_commit = (_task_meta.get("metadata") or {}).get("ground_truth_commit", "")
    effective_workspace = worktree_path or repo_path
    if pin_commit:
        try:
            git_pin_commit(effective_workspace, f"{pin_commit}^")
            logger.info(
                "[%s] Pinned workspace to %s^ (pre-merge state)",
                task_id,
                pin_commit[:8],
            )
        except subprocess.CalledProcessError as exc:
            return _error_result(
                f"Failed to pin workspace to {pin_commit[:8]}^: "
                + (exc.stderr.decode(errors="replace") if exc.stderr else str(exc)),
                error_category="system",
            )

    try:
        output = adapter.run(prompt, agent_config, session_env=session_env)
    except Exception as exc:
        return _error_result(
            sanitize_secrets(str(exc)),
            error_category=_classify_error(exc),
        )

    def _output_fields() -> dict:
        return dict(
            duration_seconds=output.duration_seconds,
            input_tokens=output.input_tokens,
            output_tokens=output.output_tokens,
            cache_read_tokens=output.cache_read_tokens,
            cost_usd=output.cost_usd,
            cost_model=output.cost_model,
            cost_source=output.cost_source,
        )

    # For oracle tasks, the agent writes answer.txt to the repo root.
    # Copy it into the task dir so the scorer's sandbox has it.
    # Done before error checks so partial results from timeouts are scored.
    effective_repo = worktree_path or repo_path
    answer_src = effective_repo / "answer.txt"
    # Also check the original repo root — agent may have followed
    # TASK_REPO_ROOT from the instruction (which points to the real repo
    # when worktree_path wasn't rewritten in older instructions).
    answer_fallback = repo_path / "answer.txt" if worktree_path else None
    found_answer = None
    if answer_src.is_file():
        found_answer = answer_src
    elif answer_fallback is not None and answer_fallback.is_file():
        found_answer = answer_fallback
    if found_answer is not None:
        try:
            shutil.copy2(found_answer, task_dir / "answer.txt")
        except OSError:
            pass  # Non-fatal; scorer will report missing answer

    # Comprehension tasks use answer.json instead of answer.txt.
    # Same copy logic: workspace root → task_dir so ArtifactScorer finds it.
    answer_json_src = effective_repo / "answer.json"
    answer_json_fallback = repo_path / "answer.json" if worktree_path else None
    found_answer_json = None
    if answer_json_src.is_file():
        found_answer_json = answer_json_src
    elif answer_json_fallback is not None and answer_json_fallback.is_file():
        found_answer_json = answer_json_fallback
    if found_answer_json is not None:
        try:
            shutil.copy2(found_answer_json, task_dir / "answer.json")
        except OSError:
            pass  # Non-fatal; scorer will report missing answer

    # If the agent failed with no output AND no answer file was produced,
    # return an error. But if an answer exists (e.g. agent timed out
    # after writing it), fall through to scoring.
    has_answer = found_answer is not None or found_answer_json is not None
    if output.exit_code != 0 and not output.stdout.strip() and not has_answer:
        error_msg = output.stderr or f"Agent exited with code {output.exit_code}"
        return TaskResult(
            completed=CompletedTask(
                task_id=task_id,
                automated_score=0.0,
                status="error",
                metadata={"error": sanitize_secrets(error_msg)},
                **_output_fields(),
            ),
            agent_stdout=output.stdout,
            agent_stderr=output.stderr or "",
        )

    try:
        scorer = get_scorer(reward_type)
    except ValueError as exc:
        return TaskResult(
            completed=CompletedTask(
                task_id=task_id,
                automated_score=0.0,
                status="error",
                metadata={"error": f"Invalid reward_type: {exc}"},
                **_output_fields(),
            ),
            agent_stdout=output.stdout,
            agent_stderr=output.stderr or "",
        )

    score_result = scorer.score(output.stdout, task_dir)

    metadata: dict = {}
    if resolved_preambles:
        metadata["resolved_preambles"] = resolved_preambles

    return TaskResult(
        completed=CompletedTask(
            task_id=task_id,
            automated_score=score_result.score,
            status="completed",
            scoring_details={
                "passed": score_result.passed,
                "error": score_result.error,
            },
            metadata=metadata,
            **_output_fields(),
        ),
        agent_stdout=output.stdout,
        agent_stderr=output.stderr or "",
    )


_BILLABLE_COST_MODELS = frozenset({"per_token"})
_BUDGET_WARNING_THRESHOLD = 0.80


def _budget_msg(msg: str) -> None:
    """Print a budget-related message to stderr so it is always visible.

    Uses sys.stderr directly rather than logger.warning() which is
    suppressed at the default INFO log level.
    """
    sys.stderr.write(f"{msg}\n")
    sys.stderr.flush()


def _get_head_ref(repo_path: Path) -> str:
    """Return the current branch name or commit SHA.

    If on a branch, returns the branch name (e.g. ``main``).
    If detached, returns the full commit SHA.
    """
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    # Detached HEAD — return commit SHA
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return "HEAD"


def _git_reset_workdir(
    repo_path: Path,
    *,
    extra_excludes: tuple[str, ...] = (),
    restore_ref: str = "",
) -> None:
    """Reset the working directory to a clean state between sequential tasks.

    Runs ``git restore .`` and ``git clean -fd`` to discard modifications
    and remove untracked files so task N's leftovers don't corrupt task N+1.

    When *restore_ref* is set, also checks out that ref to undo any commit
    pinning from the previous task.
    """
    try:
        if restore_ref:
            subprocess.run(
                ["git", "checkout", restore_ref],
                cwd=repo_path,
                check=True,
                capture_output=True,
            )
        git_restore_clean(repo_path, extra_excludes=extra_excludes)
    except subprocess.CalledProcessError as exc:
        logger.warning(
            "Git reset failed (exit %d): %s",
            exc.returncode,
            exc.stderr.decode(errors="replace") if exc.stderr else "",
        )
    except OSError as exc:
        logger.warning("Git reset failed: %s", exc)


def _save_task_artifacts(
    runs_dir: Path,
    task_id: str,
    task_result: TaskResult,
) -> None:
    """Save per-task agent output and scoring artifacts.

    Creates runs/{config_label}/{task_id}/ with:
      - agent_output.txt  — raw agent stdout (for trace/debug)
      - agent_error.txt   — raw agent stderr (only if non-empty)
      - scoring.json      — scoring details
    """
    task_dir = runs_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)

    completed = task_result.completed

    # Agent trace
    if task_result.agent_stdout:
        (task_dir / "agent_output.txt").write_text(
            sanitize_secrets(task_result.agent_stdout), encoding="utf-8"
        )
    if task_result.agent_stderr:
        (task_dir / "agent_error.txt").write_text(
            sanitize_secrets(task_result.agent_stderr), encoding="utf-8"
        )

    # Scoring details
    scoring = {
        "score": completed.automated_score,
        "status": completed.status,
        **completed.scoring_details,
    }
    (task_dir / "scoring.json").write_text(
        _json.dumps(scoring, indent=2) + "\n", encoding="utf-8"
    )


def _restore_checkpointed(
    checkpoint_store: CheckpointStore | None,
) -> tuple[set[tuple[str, int]], list[CompletedTask]]:
    """Load checkpointed results, returning (id_tuples, results).

    Each id tuple is ``(task_id, repeat_index)`` so that repeat runs
    of the same task are tracked independently.
    """
    if checkpoint_store is None:
        return set(), []
    ids: set[tuple[str, int]] = set()
    results: list[CompletedTask] = []
    for entry in checkpoint_store.load_entries():
        repeat_index = entry.get("repeat_index", 0)
        ids.add((entry["task_id"], repeat_index))
        results.append(
            CompletedTask(
                task_id=entry["task_id"],
                automated_score=entry.get("automated_score", 0.0),
                repeat_index=repeat_index,
                status=entry.get("status", "completed"),
                duration_seconds=entry.get("duration_seconds", 0.0),
                input_tokens=entry.get("input_tokens"),
                output_tokens=entry.get("output_tokens"),
                cache_read_tokens=entry.get("cache_read_tokens"),
                cost_usd=entry.get("cost_usd"),
                cost_model=entry.get("cost_model", "unknown"),
                cost_source=entry.get("cost_source", "unavailable"),
                error_category=entry.get("error_category"),
                scoring_details=entry.get("scoring_details", {}),
                metadata=entry.get("metadata", {}),
            )
        )
    return ids, results


def execute_config(
    adapter: AgentAdapter,
    task_dirs: list[Path],
    repo_path: Path,
    experiment_config: ExperimentConfig,
    agent_config: AgentConfig,
    *,
    checkpoint_store: CheckpointStore | None = None,
    runs_dir: Path | None = None,
    on_task_complete: Callable[[CompletedTask], None] | None = None,
    max_cost_usd: float | None = None,
    preamble_resolver: PreambleResolver | None = None,
    parallel: int = 1,
    isolation: IsolationStrategy | None = None,
    repeats: int = 1,
    clean_excludes: tuple[str, ...] = (),
    event_dispatcher: EventDispatcher | None = None,
) -> list[CompletedTask]:
    """Execute all tasks for a single experiment configuration.

    Resumes from checkpoint if provided. Calls on_task_complete after each task.
    Saves per-task artifacts (agent_output.txt, scoring.json) alongside the
    checkpoint file.

    When *parallel* > 1, tasks are dispatched to a thread pool.  Each agent
    subprocess runs in its own process so threads are IO-bound (waiting for
    the subprocess to finish).

    If *max_cost_usd* is set, the executor accumulates ``cost_usd`` from
    completed tasks whose ``cost_model`` is billable (currently ``per_token``).
    Once cumulative cost exceeds the budget, execution halts and partial
    results are returned.  Tasks with ``unknown`` or ``subscription``
    cost models are skipped in accumulation.

    When *event_dispatcher* is provided, lifecycle events (RunStarted,
    TaskStarted, TaskScored, RunFinished) are emitted.  If *max_cost_usd*
    is also set, a :class:`BudgetChecker` is registered to handle budget
    warnings and halt checks via the event system.
    """
    checkpointed_ids, results = _restore_checkpointed(checkpoint_store)

    # Filter checkpointed results to only include tasks in the current
    # experiment.  Without this, stale entries from prior runs with different
    # task_ids leak into the results list and inflate/deflate scores.
    current_task_ids = {d.name for d in task_dirs}
    checkpointed_ids = {
        (tid, ri) for tid, ri in checkpointed_ids if tid in current_task_ids
    }
    results = [r for r in results if r.task_id in current_task_ids]

    # Build expanded work items: (task_dir, repeat_index) for all repeats
    all_work: list[tuple[Path, int]] = [
        (d, ri) for d in task_dirs for ri in range(repeats)
    ]
    pending_work = [
        (d, ri) for d, ri in all_work if (d.name, ri) not in checkpointed_ids
    ]
    for d, ri in all_work:
        if (d.name, ri) in checkpointed_ids:
            logger.info("Skipping %s repeat %d (checkpointed)", d.name, ri)

    if not pending_work:
        return results

    # --- Event system setup ---
    budget_checker: BudgetChecker | None = None
    if event_dispatcher is not None and max_cost_usd is not None:
        budget_checker = BudgetChecker(
            budget=max_cost_usd,
            warning_threshold=_BUDGET_WARNING_THRESHOLD,
        )
        budget_checker.set_dispatcher(event_dispatcher)
        event_dispatcher.register(budget_checker)

    if event_dispatcher is not None:
        event_dispatcher.emit(
            RunStarted(
                total_tasks=len(all_work),
                config_label=experiment_config.label,
                timestamp=time.time(),
            )
        )

    run_start_time = time.time()

    cumulative_cost = 0.0

    def _run_one(
        task_dir: Path,
        repeat_index: int = 0,
        worktree_path: Path | None = None,
        session_env: dict[str, str] | None = None,
    ) -> TaskResult:
        logger.info(
            "[%s] Running %s (repeat %d)",
            experiment_config.label,
            task_dir.name,
            repeat_index,
        )
        sem = get_concurrency_semaphore()
        if sem is not None:
            sem.acquire()
        try:
            task_result = execute_task(
                adapter=adapter,
                task_dir=task_dir,
                repo_path=repo_path,
                agent_config=agent_config,
                instruction_variant=experiment_config.instruction_variant,
                reward_type=experiment_config.reward_type,
                preamble_names=experiment_config.preambles,
                preamble_resolver=preamble_resolver,
                worktree_path=worktree_path,
                session_env=session_env,
            )
            # Stamp repeat_index on the completed task
            if repeat_index != 0:
                from dataclasses import replace

                task_result = TaskResult(
                    completed=replace(task_result.completed, repeat_index=repeat_index),
                    agent_stdout=task_result.agent_stdout,
                    agent_stderr=task_result.agent_stderr,
                )
            return task_result
        finally:
            if sem is not None:
                sem.release()

    budget_warning_emitted = False

    def _handle_result(task_result: TaskResult) -> None:
        nonlocal cumulative_cost, budget_warning_emitted
        result = task_result.completed
        results.append(result)

        if runs_dir is not None:
            artifact_id = result.task_id
            if result.repeat_index > 0:
                artifact_id = f"{result.task_id}/repeat-{result.repeat_index}"
            _save_task_artifacts(runs_dir, artifact_id, task_result)

        if checkpoint_store is not None:
            checkpoint_store.append(result)

        if on_task_complete is not None:
            on_task_complete(result)

        # Emit TaskScored event when dispatcher is available
        if event_dispatcher is not None:
            event_dispatcher.emit(
                TaskScored(
                    task_id=result.task_id,
                    config_label=experiment_config.label,
                    automated_score=result.automated_score,
                    duration_seconds=result.duration_seconds,
                    cost_usd=result.cost_usd,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cache_read_tokens=result.cache_read_tokens,
                    cost_model=result.cost_model,
                    cost_source=result.cost_source,
                    error=result.metadata.get("error") if result.metadata else None,
                    timestamp=time.time(),
                )
            )

        if result.cost_model in _BILLABLE_COST_MODELS and result.cost_usd is not None:
            cumulative_cost += result.cost_usd

        # Emit 80% budget warning once (legacy path — no dispatcher)
        if (
            event_dispatcher is None
            and max_cost_usd is not None
            and not budget_warning_emitted
            and cumulative_cost >= max_cost_usd * _BUDGET_WARNING_THRESHOLD
            and cumulative_cost <= max_cost_usd
        ):
            budget_warning_emitted = True
            pct = int(cumulative_cost / max_cost_usd * 100)
            _budget_msg(
                f"Cost warning: ${cumulative_cost:.2f} of "
                f"${max_cost_usd:.2f} budget used ({pct}%)"
            )

    workers = min(parallel, len(pending_work))

    def _budget_exceeded() -> bool:
        """Check whether the cost budget has been exceeded."""
        if budget_checker is not None:
            return budget_checker.is_exceeded
        return max_cost_usd is not None and cumulative_cost > max_cost_usd

    # Capture original HEAD so we can restore it after commit pinning.
    original_ref = _get_head_ref(repo_path)

    if workers <= 1:
        # Sequential — preserves original behavior and budget checks
        for idx, (task_dir, repeat_index) in enumerate(pending_work):
            if _budget_exceeded():
                _budget_msg(
                    f"Cost budget exceeded: ${cumulative_cost:.2f} > "
                    f"${max_cost_usd:.2f} — halting"
                )
                break
            # Emit TaskStarted event
            if event_dispatcher is not None:
                event_dispatcher.emit(
                    TaskStarted(
                        task_id=task_dir.name,
                        config_label=experiment_config.label,
                        timestamp=time.time(),
                    )
                )
            # Reset working directory between tasks so leftovers from
            # task N don't corrupt task N+1's results.  Also restores
            # the original branch/HEAD in case the previous task pinned
            # to a specific commit.
            if idx > 0:
                _git_reset_workdir(
                    repo_path,
                    extra_excludes=clean_excludes,
                    restore_ref=original_ref,
                )
            task_result = _run_one(task_dir, repeat_index=repeat_index)
            _handle_result(task_result)
        # Restore original HEAD after all sequential tasks complete so
        # the repo isn't left on a detached commit from the last task.
        _git_reset_workdir(
            repo_path, extra_excludes=clean_excludes, restore_ref=original_ref
        )
    else:
        # Parallel — dispatch all pending tasks to thread pool
        logger.info(
            "[%s] Dispatching %d work items with %d workers",
            experiment_config.label,
            len(pending_work),
            workers,
        )
        # Auto-create isolation when parallel > 1 and none provided
        owns_isolation = False
        active_isolation = isolation
        if active_isolation is None:
            active_isolation = WorktreeIsolation(
                repo_path, pool_size=workers, namespace=experiment_config.label
            )
            owns_isolation = True

        def _run_isolated(task_dir: Path, repeat_index: int) -> TaskResult:
            # Emit TaskStarted event
            if event_dispatcher is not None:
                event_dispatcher.emit(
                    TaskStarted(
                        task_id=task_dir.name,
                        config_label=experiment_config.label,
                        timestamp=time.time(),
                    )
                )
            wt = active_isolation.acquire()  # type: ignore[union-attr]
            try:
                # Extract slot index from worktree path name (e.g. "slot-0" → 0)
                slot_name = wt.name
                try:
                    slot_id = int(slot_name.rsplit("-", 1)[-1])
                except (ValueError, IndexError):
                    slot_id = 0
                sess_env = adapter.isolate_session(slot_id)
                return _run_one(
                    task_dir,
                    repeat_index=repeat_index,
                    worktree_path=wt,
                    session_env=sess_env,
                )
            finally:
                active_isolation.release(wt)  # type: ignore[union-attr]

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_to_work = {
                    pool.submit(_run_isolated, td, ri): (td, ri)
                    for td, ri in pending_work
                }
                for future in as_completed(future_to_work):
                    task_dir, repeat_index = future_to_work[future]
                    try:
                        task_result = future.result()
                    except Exception as exc:
                        logger.error(
                            "[%s] %s repeat %d raised: %s",
                            experiment_config.label,
                            task_dir.name,
                            repeat_index,
                            exc,
                        )
                        task_result = TaskResult(
                            completed=CompletedTask(
                                task_id=task_dir.name,
                                automated_score=0.0,
                                repeat_index=repeat_index,
                                status="error",
                                error_category=_classify_error(exc),
                                metadata={"error": str(exc)},
                            ),
                        )
                    _handle_result(task_result)

                    if _budget_exceeded():
                        _budget_msg(
                            f"Cost budget exceeded: ${cumulative_cost:.2f} > "
                            f"${max_cost_usd:.2f} — halting"
                        )
                        for f in future_to_work:
                            f.cancel()
                        break
        finally:
            if owns_isolation:
                active_isolation.cleanup()  # type: ignore[union-attr]

    # Warn if >30% of tasks have system errors (capacity issues)
    if results:
        system_errors = sum(1 for r in results if r.error_category == "system")
        ratio = system_errors / len(results)
        if ratio > 0.30:
            logger.warning(
                "[%s] %.0f%% of tasks (%d/%d) have system errors — "
                "possible capacity issues",
                experiment_config.label,
                ratio * 100,
                system_errors,
                len(results),
            )

    # Emit RunFinished event with summary stats
    if event_dispatcher is not None:
        completed_count = len(results)
        scores = [r.automated_score for r in results]
        mean_score = sum(scores) / len(scores) if scores else 0.0
        total_cost = sum(
            r.cost_usd
            for r in results
            if r.cost_usd is not None and r.cost_model in _BILLABLE_COST_MODELS
        )
        total_duration = sum(r.duration_seconds for r in results)
        event_dispatcher.emit(
            RunFinished(
                total_tasks=len(all_work),
                completed_count=completed_count,
                mean_score=mean_score,
                total_cost=total_cost,
                total_duration=total_duration,
                timestamp=time.time(),
            )
        )

    return results
