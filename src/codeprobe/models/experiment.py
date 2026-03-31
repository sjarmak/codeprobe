"""Experiment data models — runtime state for eval experiments."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ExperimentConfig:
    """A single configuration to evaluate (e.g., 'baseline' or 'with-mcp')."""

    label: str
    agent: str = "claude"
    model: str | None = None
    permission_mode: str = "default"
    mcp_config: dict | None = None
    instruction_variant: str | None = None
    preambles: tuple[str, ...] = ()
    reward_type: str = "binary"
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CompletedTask:
    """Result of running a single task under a single configuration."""

    task_id: str
    automated_score: float
    status: str = "completed"
    duration_seconds: float = 0.0
    token_count: int | None = None
    cost_usd: float | None = None
    cost_model: str = "unknown"
    scoring_details: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ConfigResults:
    """All results for a single configuration."""

    config: str
    completed: list[CompletedTask] = field(default_factory=list)


@dataclass(frozen=True)
class Experiment:
    """Top-level experiment with metadata and configuration matrix."""

    name: str
    description: str = ""
    configs: list[ExperimentConfig] = field(default_factory=list)
    tasks_dir: str = "tasks"
