"""AgentAdapter Protocol — the extension point for adding new AI coding agents."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class AgentOutput:
    """Result from running an agent on a task."""

    stdout: str
    stderr: str | None
    exit_code: int
    duration_seconds: float
    token_count: int | None = None
    cost_usd: float | None = None


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
