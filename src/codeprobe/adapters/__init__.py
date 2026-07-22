"""Agent adapters — Protocol + built-in implementations."""

from codeprobe.adapters.protocol import (
    AdapterCapabilities,
    AdapterError,
    AdapterExecutionError,
    AdapterQuotaError,
    AdapterSetupError,
    AgentAdapter,
    AgentConfig,
    AgentOutput,
    McpInitManifest,
    McpServerStatus,
    capabilities_of,
)

__all__ = [
    "AdapterCapabilities",
    "AdapterError",
    "AdapterExecutionError",
    "AdapterQuotaError",
    "AdapterSetupError",
    "AgentAdapter",
    "AgentConfig",
    "AgentOutput",
    "McpInitManifest",
    "McpServerStatus",
    "capabilities_of",
]
