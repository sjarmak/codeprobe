"""Task execution — run agents on tasks and collect results."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from codeprobe.core.experiment import append_checkpoint, load_checkpoint_entries
from codeprobe.core.scoring import sanitize_secrets, score_task_output
from codeprobe.models.experiment import CompletedTask, ExperimentConfig

if TYPE_CHECKING:
    from codeprobe.adapters.protocol import AgentAdapter, AgentConfig

logger = logging.getLogger(__name__)


def build_prompt(instruction: str, repo_path: Path) -> str:
    """Build the prompt sent to an agent."""
    return (
        f"You are working on the repository at {repo_path}. "
        "Follow the instruction below.\n\n"
        f"{instruction}"
    )


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
) -> CompletedTask:
    """Execute a single task and return a CompletedTask.

    Never raises — errors are captured in the result metadata.
    """
    task_id = task_dir.name

    try:
        instruction = load_instruction(task_dir, variant=instruction_variant)
    except FileNotFoundError as exc:
        return CompletedTask(
            task_id=task_id,
            automated_score=0.0,
            status="error",
            metadata={"error": str(exc)},
        )

    prompt = build_prompt(instruction, repo_path)

    try:
        output = adapter.run(prompt, agent_config)
    except Exception as exc:
        return CompletedTask(
            task_id=task_id,
            automated_score=0.0,
            status="error",
            metadata={"error": sanitize_secrets(str(exc))},
        )

    if output.exit_code != 0 and not output.stdout.strip():
        error_msg = output.stderr or f"Agent exited with code {output.exit_code}"
        return CompletedTask(
            task_id=task_id,
            automated_score=0.0,
            status="error",
            duration_seconds=output.duration_seconds,
            token_count=output.token_count,
            cost_usd=output.cost_usd,
            cost_model=output.cost_model,
            metadata={"error": sanitize_secrets(error_msg)},
        )

    score_result = score_task_output(output.stdout, task_dir)

    return CompletedTask(
        task_id=task_id,
        automated_score=score_result.score,
        status="completed",
        duration_seconds=output.duration_seconds,
        token_count=output.token_count,
        cost_usd=output.cost_usd,
        cost_model=output.cost_model,
        scoring_details={"passed": score_result.passed, "error": score_result.error},
    )


_BILLABLE_COST_MODELS = frozenset({"per_token"})


def execute_config(
    adapter: AgentAdapter,
    task_dirs: list[Path],
    repo_path: Path,
    experiment_config: ExperimentConfig,
    agent_config: AgentConfig,
    *,
    checkpoint_path: Path | None = None,
    on_task_complete: Callable[[CompletedTask], None] | None = None,
    max_cost_usd: float | None = None,
) -> list[CompletedTask]:
    """Execute all tasks for a single experiment configuration.

    Resumes from checkpoint if provided. Calls on_task_complete after each task.

    If *max_cost_usd* is set, the executor accumulates ``cost_usd`` from
    completed tasks whose ``cost_model`` is billable (currently ``per_token``).
    Once cumulative cost exceeds the budget, execution halts and partial
    results are returned.  Tasks with ``unknown`` or ``subscription``
    cost models are skipped in accumulation.
    """
    results: list[CompletedTask] = []
    checkpointed_ids: set[str] = set()
    if checkpoint_path is not None:
        for entry in load_checkpoint_entries(checkpoint_path):
            checkpointed_ids.add(entry["task_id"])
            results.append(CompletedTask(
                task_id=entry["task_id"],
                automated_score=entry.get("automated_score", 0.0),
            ))

    cumulative_cost = 0.0

    for task_dir in task_dirs:
        task_id = task_dir.name

        if task_id in checkpointed_ids:
            logger.info("Skipping %s (checkpointed)", task_id)
            continue

        # Check budget *before* dispatching the next task
        if max_cost_usd is not None and cumulative_cost > max_cost_usd:
            logger.warning(
                "Cost circuit-breaker: cumulative $%.2f exceeds budget $%.2f — "
                "halting after %d/%d tasks",
                cumulative_cost,
                max_cost_usd,
                len(results) - len(checkpointed_ids),
                len(task_dirs),
            )
            break

        logger.info("[%s] Running %s", experiment_config.label, task_id)

        result = execute_task(
            adapter=adapter,
            task_dir=task_dir,
            repo_path=repo_path,
            agent_config=agent_config,
            instruction_variant=experiment_config.instruction_variant,
        )

        results.append(result)

        if checkpoint_path is not None:
            append_checkpoint(checkpoint_path, result)

        if on_task_complete is not None:
            on_task_complete(result)

        # Accumulate cost only for billable cost models with known cost
        if result.cost_model in _BILLABLE_COST_MODELS and result.cost_usd is not None:
            cumulative_cost += result.cost_usd

    return results
