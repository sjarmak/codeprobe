"""Experiment data models — runtime state for eval experiments."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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

    def __repr__(self) -> str:
        # Lazy import: config/__init__.py → loader.py → this module (circular)
        from codeprobe.config.redact import redact_mcp_headers

        redacted_mcp = redact_mcp_headers(self.mcp_config)
        return (
            f"ExperimentConfig(label={self.label!r}, agent={self.agent!r}, "
            f"model={self.model!r}, permission_mode={self.permission_mode!r}, "
            f"mcp_config={redacted_mcp!r}, "
            f"instruction_variant={self.instruction_variant!r}, "
            f"preambles={self.preambles!r}, reward_type={self.reward_type!r}, "
            f"extra={self.extra!r})"
        )


@dataclass(frozen=True)
class DualScoringDetails:
    """Dual scoring details capturing direct and artifact-based scores.

    Note: This dataclass is a typed view over the plain dict stored in
    ``CompletedTask.scoring_details``. The on-the-wire representation remains
    a ``dict`` to preserve checkpoint serialization compatibility; use
    :meth:`from_dict` / :meth:`to_dict` to convert between the two.
    """

    score_direct: float = 0.0
    score_artifact: float = 0.0
    passed_direct: bool = False
    passed_artifact: bool = False
    scoring_policy: str = ""
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DualScoringDetails:
        """Build an instance from a plain dict, tolerating missing keys."""
        return cls(
            score_direct=float(d.get("score_direct", 0.0)),
            score_artifact=float(d.get("score_artifact", 0.0)),
            passed_direct=bool(d.get("passed_direct", False)),
            passed_artifact=bool(d.get("passed_artifact", False)),
            scoring_policy=str(d.get("scoring_policy", "")),
            extra=dict(d.get("extra", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return a plain dict round-trippable through :meth:`from_dict`."""
        return asdict(self)


@dataclass(frozen=True)
class CompletedTask:
    """Result of running a single task under a single configuration."""

    task_id: str
    automated_score: float
    repeat_index: int = 0
    status: str = "completed"
    duration_seconds: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_usd: float | None = None
    cost_model: str = "unknown"
    cost_source: str = "unavailable"
    tool_call_count: int | None = None
    error_category: str | None = None
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
    task_ids: tuple[str, ...] = ()
