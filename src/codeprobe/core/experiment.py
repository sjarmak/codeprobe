"""Experiment directory I/O — create, save, load, checkpoint."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from codeprobe.models.experiment import (
    CompletedTask,
    ConfigResults,
    Experiment,
    ExperimentConfig,
)

logger = logging.getLogger(__name__)


def create_experiment_dir(base_dir: Path, experiment: Experiment) -> Path:
    """Create the experiment directory structure and write experiment.json.

    Returns the experiment directory path.
    """
    exp_dir = base_dir / experiment.name
    (exp_dir / "tasks").mkdir(parents=True, exist_ok=True)

    for config in experiment.configs:
        (exp_dir / "runs" / config.label).mkdir(parents=True, exist_ok=True)

    save_experiment(exp_dir, experiment)
    return exp_dir


def save_experiment(exp_dir: Path, experiment: Experiment) -> None:
    """Write experiment.json to the experiment directory."""
    data = {
        "name": experiment.name,
        "description": experiment.description,
        "tasks_dir": experiment.tasks_dir,
        "configs": [asdict(c) for c in experiment.configs],
    }
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
            mcp_config=c.get("mcp_config"),
            instruction_variant=c.get("instruction_variant"),
            extra=c.get("extra", {}),
        )
        for c in data.get("configs", [])
    ]

    return Experiment(
        name=name,
        description=data.get("description", ""),
        configs=configs,
        tasks_dir=data.get("tasks_dir", "tasks"),
    )


def save_config_results(
    exp_dir: Path,
    config_label: str,
    completed: Sequence[CompletedTask],
) -> Path:
    """Write results.json for a configuration.

    Returns the path to the written file.
    """
    results_dir = exp_dir / "runs" / config_label
    results_dir.mkdir(parents=True, exist_ok=True)

    data = {
        "config": config_label,
        "completed": [asdict(t) for t in completed],
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
            status=t.get("status", "completed"),
            duration_seconds=t.get("duration_seconds", 0.0),
            token_count=t.get("token_count"),
            cost_usd=t.get("cost_usd"),
            scoring_details=t.get("scoring_details", {}),
            metadata=t.get("metadata", {}),
        )
        for t in data.get("completed", [])
    ]

    return ConfigResults(config=data["config"], completed=completed)


def append_checkpoint(checkpoint_path: Path, task: CompletedTask) -> None:
    """Append a completed task as a JSONL line to the checkpoint file."""
    entry = {"task_id": task.task_id, "automated_score": task.automated_score}
    with open(checkpoint_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def load_checkpoint(checkpoint_path: Path) -> set[str]:
    """Load completed task IDs from a JSONL checkpoint file.

    Returns an empty set if the file doesn't exist. Skips malformed lines.
    """
    entries = load_checkpoint_entries(checkpoint_path)
    return {e["task_id"] for e in entries}


def load_checkpoint_entries(checkpoint_path: Path) -> list[dict]:
    """Load all checkpoint entries from a JSONL file.

    Returns an empty list if the file doesn't exist. Skips malformed lines.
    """
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
