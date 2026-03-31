"""Core pipeline — experiment management, agent execution, scoring."""

from codeprobe.core.executor import execute_config, execute_task
from codeprobe.core.experiment import (
    create_experiment_dir,
    load_config_results,
    load_experiment,
    save_config_results,
    save_experiment,
)
from codeprobe.core.registry import available as available_agents
from codeprobe.core.registry import resolve as resolve_agent
from codeprobe.core.scoring import score_task_output

__all__ = [
    "available_agents",
    "create_experiment_dir",
    "execute_config",
    "execute_task",
    "load_config_results",
    "load_experiment",
    "resolve_agent",
    "save_config_results",
    "save_experiment",
    "score_task_output",
]
