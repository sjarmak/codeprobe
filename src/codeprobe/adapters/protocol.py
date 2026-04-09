"""AgentAdapter Protocol — the extension point for adding new AI coding agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

ALLOWED_PERMISSION_MODES = frozenset(
    {"default", "plan", "auto", "acceptEdits", "dangerously_skip"}
)
ALLOWED_COST_MODELS = frozenset({"per_token", "subscription", "unknown"})
ALLOWED_COST_SOURCES = frozenset(
    {"api_reported", "calculated", "log_parsed", "estimated", "unavailable"}
)


# -- Error hierarchy ----------------------------------------------------------


class AdapterError(Exception):
    """Base exception for all adapter errors."""


class AdapterSetupError(AdapterError):
    """Binary not found, auth missing, or other pre-run setup failure."""


class AdapterExecutionError(AdapterError):
    """Unrecoverable failure during agent execution."""


@dataclass(frozen=True)
class AgentOutput:
    """Result from running an agent on a task."""

    stdout: str
    stderr: str | None
    exit_code: int
    duration_seconds: float
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_model: str = "unknown"
    error: str | None = None
    cost_source: str = "unavailable"

    def __post_init__(self) -> None:
        if self.cost_model not in ALLOWED_COST_MODELS:
            raise ValueError(
                f"Unknown cost_model: {self.cost_model!r}. "
                f"Expected one of: {sorted(ALLOWED_COST_MODELS)}"
            )
        if self.cost_model == "per_token" and self.cost_usd is None:
            raise ValueError("cost_usd is required when cost_model is 'per_token'")
        if self.cost_source not in ALLOWED_COST_SOURCES:
            raise ValueError(
                f"Unknown cost_source: {self.cost_source!r}. "
                f"Expected one of: {sorted(ALLOWED_COST_SOURCES)}"
            )


@dataclass(frozen=True)
class AgentConfig:
    """Configuration passed to an agent adapter."""

    model: str | None = None
    permission_mode: str = "default"
    timeout_seconds: int = 300
    mcp_config: dict | None = None
    extra: dict | None = None
    cwd: str | None = None


@runtime_checkable
class AgentAdapter(Protocol):
    """Protocol for AI coding agent adapters.

    Implement this to add support for a new agent (Aider, Cursor, Codex, etc.).
    Register via entry_points in pyproject.toml:

        [project.entry-points."codeprobe.agents"]
        myagent = "my_package:MyAgentAdapter"

    For cross-repo tasks, the executor may lay out additional
    repositories under ``<workspace>/repos/<name>``, each pinned to its
    own pre-merge commit.  Adapters don't need special handling — the
    paths are available for the model to navigate, and the primary
    workspace remains at its existing location for backwards
    compatibility with single-repo tasks.
    """

    @property
    def name(self) -> str:
        """Human-readable agent name."""
        ...

    def preflight(self, config: AgentConfig) -> list[str]:
        """Validate that the agent is ready to run.

        Returns a list of issues (empty = ready).
        """
        ...

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
        """Execute the agent with the given prompt and return results."""
        ...

    def isolate_session(self, slot_id: int) -> dict[str, str]:
        """Return per-slot env overrides for session isolation.

        Adapters that need session-level isolation (e.g. separate config dirs)
        return a dict of env vars.  Default: empty dict (no isolation).
        """
        ...
