"""EvalRC configuration model — the user-facing .evalrc.yaml schema."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class EvalrcConfig:
    """Parsed .evalrc.yaml configuration."""

    name: str = "default"
    description: str = ""
    tasks_dir: str = "tasks"
    agents: list[str] = field(default_factory=lambda: ["claude"])
    models: list[str] = field(default_factory=list)
    configs: dict = field(default_factory=dict)
    dimensions: dict = field(default_factory=dict)
