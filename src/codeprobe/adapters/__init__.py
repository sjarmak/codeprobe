"""Agent adapters — Protocol + built-in implementations."""

from codeprobe.adapters.protocol import (
    AdapterError,
    AdapterExecutionError,
    AdapterSetupError,
    AgentAdapter,
    AgentConfig,
    AgentOutput,
)

__all__ = [
    "AdapterError",
    "AdapterExecutionError",
    "AdapterSetupError",
    "AgentAdapter",
    "AgentConfig",
    "AgentOutput",
]
