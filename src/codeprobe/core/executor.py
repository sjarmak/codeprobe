"""Task execution — run agents on tasks and collect results."""

from __future__ import annotations

import json as _json
import logging
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from codeprobe.core.checkpoint import CheckpointStore
from codeprobe.core.isolation import IsolationStrategy, WorktreeIsolation
from codeprobe.core.preamble import PreambleResolver, _base_prompt, compose_instruction
from codeprobe.core.scoring import get_scorer, sanitize_secrets
from codeprobe.models.experiment import CompletedTask, ExperimentConfig

if TYPE_CHECKING:
    from codeprobe.adapters.protocol import AgentAdapter, AgentConfig

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
) -> TaskResult:
    """Execute a single task and return a TaskResult with trace data.

    Never raises — errors are captured in the result metadata.
    """
    task_id = task_dir.name

    def _error_result(error: str) -> TaskResult:
        return TaskResult(
            completed=CompletedTask(
                task_id=task_id,
                automated_score=0.0,
                status="error",
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
        try:
            prompt, resolved_preambles = compose_instruction(
                instruction,
                repo_path,
                preamble_names=list(preamble_names),
                resolver=preamble_resolver,
                task_id=task_id,
                worktree_path=worktree_path,
            )
        except (FileNotFoundError, ValueError) as exc:
            return _error_result(f"Preamble resolution failed: {exc}")
    else:
        prompt = _base_prompt(instruction, repo_path, worktree_path=worktree_path)

    try:
        output = adapter.run(prompt, agent_config)
    except Exception as exc:
        return _error_result(sanitize_secrets(str(exc)))

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

    if output.exit_code != 0 and not output.stdout.strip():
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


def _git_reset_workdir(repo_path: Path) -> None:
    """Reset the working directory to a clean state between sequential tasks.

    Runs ``git checkout -- .`` and ``git clean -fd`` to discard modifications
    and remove untracked files so task N's leftovers don't corrupt task N+1.
    """
    try:
        subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=repo_path,
            check=True,
            capture_output=True,
        )
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
) -> tuple[set[str], list[CompletedTask]]:
    """Load checkpointed results, returning (ids, results)."""
    if checkpoint_store is None:
        return set(), []
    ids: set[str] = set()
    results: list[CompletedTask] = []
    for entry in checkpoint_store.load_entries():
        ids.add(entry["task_id"])
        results.append(
            CompletedTask(
                task_id=entry["task_id"],
                automated_score=entry.get("automated_score", 0.0),
                status=entry.get("status", "completed"),
                duration_seconds=entry.get("duration_seconds", 0.0),
                input_tokens=entry.get("input_tokens"),
                output_tokens=entry.get("output_tokens"),
                cache_read_tokens=entry.get("cache_read_tokens"),
                cost_usd=entry.get("cost_usd"),
                cost_model=entry.get("cost_model", "unknown"),
                cost_source=entry.get("cost_source", "unavailable"),
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
    """
    checkpointed_ids, results = _restore_checkpointed(checkpoint_store)

    pending_dirs = [d for d in task_dirs if d.name not in checkpointed_ids]
    for skipped in task_dirs:
        if skipped.name in checkpointed_ids:
            logger.info("Skipping %s (checkpointed)", skipped.name)

    if not pending_dirs:
        return results

    cumulative_cost = 0.0

    def _run_one(task_dir: Path, worktree_path: Path | None = None) -> TaskResult:
        logger.info("[%s] Running %s", experiment_config.label, task_dir.name)
        sem = get_concurrency_semaphore()
        if sem is not None:
            sem.acquire()
        try:
            return execute_task(
                adapter=adapter,
                task_dir=task_dir,
                repo_path=repo_path,
                agent_config=agent_config,
                instruction_variant=experiment_config.instruction_variant,
                reward_type=experiment_config.reward_type,
                preamble_names=experiment_config.preambles,
                preamble_resolver=preamble_resolver,
                worktree_path=worktree_path,
            )
        finally:
            if sem is not None:
                sem.release()

    def _handle_result(task_result: TaskResult) -> None:
        nonlocal cumulative_cost
        result = task_result.completed
        results.append(result)

        if runs_dir is not None:
            _save_task_artifacts(runs_dir, result.task_id, task_result)

        if checkpoint_store is not None:
            checkpoint_store.append(result)

        if on_task_complete is not None:
            on_task_complete(result)

        if result.cost_model in _BILLABLE_COST_MODELS and result.cost_usd is not None:
            cumulative_cost += result.cost_usd

    workers = min(parallel, len(pending_dirs))

    if workers <= 1:
        # Sequential — preserves original behavior and budget checks
        for idx, task_dir in enumerate(pending_dirs):
            if max_cost_usd is not None and cumulative_cost > max_cost_usd:
                logger.warning(
                    "Cost circuit-breaker: $%.2f exceeds budget $%.2f — halting",
                    cumulative_cost,
                    max_cost_usd,
                )
                break
            # Reset working directory between tasks so leftovers from
            # task N don't corrupt task N+1's results.
            if idx > 0:
                _git_reset_workdir(repo_path)
            task_result = _run_one(task_dir)
            _handle_result(task_result)
    else:
        # Parallel — dispatch all pending tasks to thread pool
        logger.info(
            "[%s] Dispatching %d tasks with %d workers",
            experiment_config.label,
            len(pending_dirs),
            workers,
        )
        # Auto-create isolation when parallel > 1 and none provided
        owns_isolation = False
        active_isolation = isolation
        if active_isolation is None:
            active_isolation = WorktreeIsolation(repo_path, pool_size=workers)
            owns_isolation = True

        def _run_isolated(task_dir: Path) -> TaskResult:
            wt = active_isolation.acquire()  # type: ignore[union-attr]
            try:
                return _run_one(task_dir, worktree_path=wt)
            finally:
                active_isolation.release(wt)  # type: ignore[union-attr]

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                future_to_dir = {
                    pool.submit(_run_isolated, td): td for td in pending_dirs
                }
                for future in as_completed(future_to_dir):
                    task_dir = future_to_dir[future]
                    try:
                        task_result = future.result()
                    except Exception as exc:
                        logger.error(
                            "[%s] %s raised: %s",
                            experiment_config.label,
                            task_dir.name,
                            exc,
                        )
                        task_result = TaskResult(
                            completed=CompletedTask(
                                task_id=task_dir.name,
                                automated_score=0.0,
                                status="error",
                                metadata={"error": str(exc)},
                            ),
                        )
                    _handle_result(task_result)

                    if max_cost_usd is not None and cumulative_cost > max_cost_usd:
                        logger.warning(
                            "Cost circuit-breaker: $%.2f exceeds budget $%.2f — "
                            "cancelling remaining tasks",
                            cumulative_cost,
                            max_cost_usd,
                        )
                        for f in future_to_dir:
                            f.cancel()
                        break
        finally:
            if owns_isolation:
                active_isolation.cleanup()  # type: ignore[union-attr]

    return results
