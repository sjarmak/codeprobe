"""AgentAdapter Protocol — the extension point for adding new AI coding agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

ALLOWED_PERMISSION_MODES = frozenset({"default", "plan", "auto", "acceptEdits"})
ALLOWED_COST_MODELS = frozenset({"per_token", "subscription", "unknown"})


@dataclass(frozen=True)
class AgentOutput:
    """Result from running an agent on a task."""

    stdout: str
    stderr: str | None
    exit_code: int
    duration_seconds: float
    token_count: int | None = None
    cost_usd: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cost_model: str = "unknown"

    def __post_init__(self) -> None:
        if self.cost_model not in ALLOWED_COST_MODELS:
            raise ValueError(
                f"Unknown cost_model: {self.cost_model!r}. "
                f"Expected one of: {sorted(ALLOWED_COST_MODELS)}"
            )
        if self.cost_model == "per_token" and self.cost_usd is None:
            raise ValueError("cost_usd is required when cost_model is 'per_token'")


@dataclass(frozen=True)
class AgentConfig:
    """Configuration passed to an agent adapter."""

    model: str | None = None
    permission_mode: str = "default"
    timeout_seconds: int = 300
    mcp_config: dict | None = None
    extra: dict | None = None


@runtime_checkable
class AgentAdapter(Protocol):
    """Protocol for AI coding agent adapters.

    Implement this to add support for a new agent (Aider, Cursor, Codex, etc.).
    Register via entry_points in pyproject.toml:

        [project.entry-points."codeprobe.agents"]
        myagent = "my_package:MyAgentAdapter"
    """

    @property
    def name(self) -> str:
        """Human-readable agent name."""
        ...

    def find_binary(self) -> str | None:
        """Return path to the agent CLI binary, or None if not found."""
        ...

    def preflight(self, config: AgentConfig) -> list[str]:
        """Validate that the agent is ready to run.

        Returns a list of issues (empty = ready).
        """
        ...

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        """Build the shell command to execute the agent."""
        ...

    def run(self, prompt: str, config: AgentConfig) -> AgentOutput:
        """Execute the agent with the given prompt and return results."""
        ...
