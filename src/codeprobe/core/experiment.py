"""Experiment directory I/O — create, save, load, checkpoint."""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from codeprobe.config.redact import redact_mcp_headers
from codeprobe.models.experiment import (
    CompletedTask,
    ConfigResults,
    Experiment,
    ExperimentConfig,
)

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


def _validate_path_component(value: str, field: str) -> None:
    """Validate that *value* is a safe path component (no traversal)."""
    if not _SAFE_NAME.match(value):
        raise ValueError(
            f"Unsafe {field}: {value!r}. "
            "Use only letters, digits, hyphens, underscores, and dots."
        )


def create_experiment_dir(base_dir: Path, experiment: Experiment) -> Path:
    """Create the experiment directory structure and write experiment.json.

    Returns the experiment directory path.
    """
    _validate_path_component(experiment.name, "experiment name")
    for config in experiment.configs:
        _validate_path_component(config.label, "config label")

    exp_dir = base_dir / experiment.name
    (exp_dir / "tasks").mkdir(parents=True, exist_ok=True)

    # Ensure .codeprobe/ is excluded from git in the target repo.
    # base_dir is typically <repo>/.codeprobe — walk up to the repo root.
    from codeprobe.core.repo_hygiene import ensure_codeprobe_excluded

    if base_dir.name == ".codeprobe":
        ensure_codeprobe_excluded(base_dir.parent)

    for config in experiment.configs:
        (exp_dir / "runs" / config.label).mkdir(parents=True, exist_ok=True)

    save_experiment(exp_dir, experiment)
    return exp_dir


def save_experiment(exp_dir: Path, experiment: Experiment) -> None:
    """Write experiment.json to the experiment directory."""
    serialized_configs = []
    for c in experiment.configs:
        d = asdict(c)
        d["mcp_config"] = redact_mcp_headers(c.mcp_config)
        serialized_configs.append(d)
    data: dict = {
        "name": experiment.name,
        "description": experiment.description,
        "tasks_dir": experiment.tasks_dir,
        "configs": serialized_configs,
    }
    if experiment.task_ids:
        data["task_ids"] = list(experiment.task_ids)
    path = exp_dir / "experiment.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def load_experiment(exp_dir: Path) -> Experiment:
    """Load an Experiment from experiment.json.

    Raises FileNotFoundError if the directory or file doesn't exist.
    """
    path = exp_dir / "experiment.json"
    if not path.is_file():
        raise FileNotFoundError(f"Experiment not found: {path}")

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        name = data["name"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise ValueError(f"Invalid experiment.json at {path}: {exc}") from exc

    configs = [
        ExperimentConfig(
            label=c["label"],
            agent=c.get("agent", "claude"),
            model=c.get("model"),
            permission_mode=c.get("permission_mode", "default"),
            mcp_config=c.get("mcp_config"),
            instruction_variant=c.get("instruction_variant"),
            extra=c.get("extra", {}),
        )
        for c in data.get("configs", [])
    ]

    tasks_dir = data.get("tasks_dir", "tasks")
    # Validate all path components from the untrusted experiment.json
    _validate_path_component(name, "experiment name")
    _validate_path_component(tasks_dir, "tasks_dir")
    for c in configs:
        _validate_path_component(c.label, "config label")

    task_ids = tuple(data.get("task_ids", ()))

    return Experiment(
        name=name,
        description=data.get("description", ""),
        configs=configs,
        tasks_dir=tasks_dir,
        task_ids=task_ids,
    )


def _compute_summary(completed: Sequence[CompletedTask]) -> dict:
    """Compute aggregate summary from completed tasks.

    Follows the MCP-Eval-Tasks pattern: mean score, total cost, total tokens.
    Single-pass over the sequence.
    """
    if not completed:
        return {}

    n = 0
    score_sum = 0.0
    cost_sum = 0.0
    has_cost = False
    input_sum = 0
    has_input = False
    output_sum = 0
    has_output = False
    cache_sum = 0
    has_cache = False
    dur_sum = 0.0

    for t in completed:
        n += 1
        score_sum += t.automated_score
        dur_sum += t.duration_seconds
        if t.cost_usd is not None:
            cost_sum += t.cost_usd
            has_cost = True
        if t.input_tokens is not None:
            input_sum += t.input_tokens
            has_input = True
        if t.output_tokens is not None:
            output_sum += t.output_tokens
            has_output = True
        if t.cache_read_tokens is not None:
            cache_sum += t.cache_read_tokens
            has_cache = True

    mean_score = score_sum / n
    total_cost = cost_sum if has_cost else None

    summary: dict = {
        "tasks_completed": n,
        "mean_automated_score": round(mean_score, 4),
        "total_duration_seconds": round(dur_sum, 1),
        "total_tokens": {
            "input": input_sum if has_input else None,
            "output": output_sum if has_output else None,
            "cache_read": cache_sum if has_cache else None,
        },
        "total_cost_usd": round(total_cost, 6) if total_cost is not None else None,
    }

    if total_cost and total_cost > 0:
        summary["score_per_dollar"] = round(mean_score / (total_cost / n), 2)

    return summary


def save_config_results(
    exp_dir: Path,
    config_label: str,
    completed: Sequence[CompletedTask],
) -> Path:
    """Write results.json for a configuration.

    Includes a summary block with aggregate metrics (mean score, total cost,
    total tokens) following the MCP-Eval-Tasks pattern.

    Returns the path to the written file.
    """
    results_dir = exp_dir / "runs" / config_label
    results_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "config": config_label,
        "completed": [asdict(t) for t in completed],
        "summary": _compute_summary(completed),
    }

    path = results_dir / "results.json"
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def load_config_results(exp_dir: Path, config_label: str) -> ConfigResults:
    """Load results.json for a configuration.

    Raises FileNotFoundError if the results file doesn't exist.
    """
    path = exp_dir / "runs" / config_label / "results.json"
    if not path.is_file():
        raise FileNotFoundError(f"Results not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))

    completed = [
        CompletedTask(
            task_id=t["task_id"],
            automated_score=t["automated_score"],
            repeat_index=t.get("repeat_index", 0),
            status=t.get("status", "completed"),
            duration_seconds=t.get("duration_seconds", 0.0),
            input_tokens=t.get("input_tokens"),
            output_tokens=t.get("output_tokens"),
            cache_read_tokens=t.get("cache_read_tokens"),
            cost_usd=t.get("cost_usd"),
            cost_model=t.get("cost_model", "unknown"),
            cost_source=t.get("cost_source", "unavailable"),
            tool_call_count=t.get("tool_call_count"),
            scoring_details=t.get("scoring_details", {}),
            metadata=t.get("metadata", {}),
        )
        for t in data.get("completed", [])
    ]

    return ConfigResults(config=data["config"], completed=completed)


def append_checkpoint(checkpoint_path: Path, task: CompletedTask) -> None:
    """Append a completed task as a JSONL line to the checkpoint file.

    .. deprecated:: Use :class:`codeprobe.core.checkpoint.CheckpointStore` instead.
    """
    import warnings

    warnings.warn(
        "append_checkpoint is deprecated, use CheckpointStore.append()",
        DeprecationWarning,
        stacklevel=2,
    )
    entry = {"task_id": task.task_id, "automated_score": task.automated_score}
    with open(checkpoint_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def load_checkpoint(checkpoint_path: Path) -> set[str]:
    """Load completed task IDs from a JSONL checkpoint file.

    .. deprecated:: Use :class:`codeprobe.core.checkpoint.CheckpointStore` instead.

    Returns an empty set if the file doesn't exist. Skips malformed lines.
    """
    import warnings

    warnings.warn(
        "load_checkpoint is deprecated, use CheckpointStore.load_ids()",
        DeprecationWarning,
        stacklevel=2,
    )
    entries = load_checkpoint_entries(checkpoint_path)
    return {e["task_id"] for e in entries}


def load_checkpoint_entries(checkpoint_path: Path) -> list[dict]:
    """Load all checkpoint entries from a JSONL file.

    .. deprecated:: Use :class:`codeprobe.core.checkpoint.CheckpointStore` instead.

    Returns an empty list if the file doesn't exist. Skips malformed lines.
    """
    import warnings

    warnings.warn(
        "load_checkpoint_entries is deprecated, use CheckpointStore.load_entries()",
        DeprecationWarning,
        stacklevel=2,
    )
    if not checkpoint_path.is_file():
        return []

    entries: list[dict] = []
    with open(checkpoint_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if "task_id" in entry:
                    entries.append(entry)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed checkpoint line: %s", line[:80])
    return entries
