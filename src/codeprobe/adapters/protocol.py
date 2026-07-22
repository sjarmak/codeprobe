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
class AdapterCapabilities:
    """Which :class:`AgentConfig` knobs an adapter actually honors.

    Every field defaults to ``False`` because the contract is FAIL-CLOSED:
    an adapter that does not declare a capability is treated as supporting
    nothing beyond prompt+model. The run preflight hard-refuses any
    experiment arm that requests a knob its adapter does not cover —
    otherwise the knob silently no-ops and the A/B report labels arms as
    different configs while the numbers compare nothing (codeprobe-f7rl.26).

    Declarations must match the adapter's actual ``config.*`` usage
    (grep-verified in review), never its aspirations.
    """

    mcp_config: bool = False
    allowed_tools: bool = False
    disallowed_tools: bool = False
    max_turns: bool = False
    permission_mode: bool = False
    workspace_cwd: bool = False
    timeout: bool = False


def capabilities_of(adapter: object) -> AdapterCapabilities:
    """Return *adapter*'s declared capabilities, fail-closed.

    An adapter without a ``capabilities`` attribute — or with one of the
    wrong type — is treated as supporting nothing beyond prompt+model,
    so third-party adapters that predate the declaration are refused any
    knob rather than silently dropping it.
    """
    caps = getattr(adapter, "capabilities", None)
    if isinstance(caps, AdapterCapabilities):
        return caps
    return AdapterCapabilities()


@dataclass(frozen=True)
class McpServerStatus:
    """Attach status of a single MCP server, verbatim from the CLI init event.

    ``status`` is the raw string the agent CLI reported (Claude emits e.g.
    ``"connected"`` / ``"failed"`` / ``"pending"``). It is NOT normalised so
    an arm-level audit can read the exact attach outcome.
    """

    name: str
    status: str


@dataclass(frozen=True)
class McpInitManifest:
    """The tool surface actually offered to the agent for one trial.

    Captured from the ``--output-format stream-json --verbose`` ``init`` /
    ``system`` event the CLI emits before the first turn. This is the
    zero-inference record of which tools and MCP servers were *available*
    in an arm (codeprobe-9p6): a server that silently fails to attach is
    distinguishable from an agent that simply chose not to call it.

    ``captured`` is ``False`` when no init event was found in the stream
    (single-envelope ``--output-format json``, a quota stub, or a crashed
    run). The empty manifest is recorded explicitly rather than dropped, so
    "no tools offered" is never confused with "tools not measured"
    (adapter-contract: preserve partial data with an error field).
    """

    captured: bool
    offered_tools: tuple[str, ...] = ()
    mcp_servers: tuple[McpServerStatus, ...] = ()

    @property
    def mcp_tools(self) -> tuple[str, ...]:
        """The ``mcp__<server>__<tool>`` subset of the offered tools."""
        return tuple(t for t in self.offered_tools if t.startswith("mcp__"))

    @property
    def failed_servers(self) -> tuple[McpServerStatus, ...]:
        """Configured servers the CLI could not attach (status != connected)."""
        return tuple(s for s in self.mcp_servers if s.status != "connected")

    def to_dict(self) -> dict:
        """Plain-dict form for per-trial artifacts and checkpoint storage."""
        return {
            "captured": self.captured,
            "offered_tools": list(self.offered_tools),
            "mcp_tools": list(self.mcp_tools),
            "mcp_servers": [
                {"name": s.name, "status": s.status} for s in self.mcp_servers
            ],
            "failed_servers": [s.name for s in self.failed_servers],
        }


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
    cache_creation_tokens: int | None = None
    cost_model: str = "unknown"
    error: str | None = None
    # Optional adapter-declared category for ``error``. When the adapter
    # can identify a recoverable-vs-unrecoverable / quota / auth class,
    # it sets this so the executor can route the failure correctly.
    # Recognised values include "quota" (codeprobe-9xrl). None means
    # "use the executor's default classification".
    error_category: str | None = None
    # Adapter-declared: ``error`` is a TERMINAL agent outcome (the agent
    # ran to a protocol-defined stop condition such as a turn cap, so the
    # 0.0 reward is a genuine measurement) rather than an infra casualty.
    # The executor maps this to status='failed', which checkpoint resume
    # keeps instead of retrying (codeprobe-8up). Adapters must only set
    # this for stop conditions they positively recognise.
    error_terminal: bool = False
    cost_source: str = "unavailable"
    tool_call_count: int | None = None
    # Per-tool usage counts (e.g. {"Read": 5, "mcp__sourcegraph__...": 2}).
    # None when the adapter couldn't capture a streaming transcript.
    tool_use_by_name: dict[str, int] | None = None
    # CLI result-record fields (codeprobe-8up). Populated when the agent
    # CLI emits a terminal result envelope (Claude CLI ``type: "result"``);
    # None when no such record exists (quota stubs, crashes, adapters
    # that don't surface one). ``result_subtype`` is the verbatim protocol
    # value (e.g. "success", "error_max_turns") — the executor uses it to
    # distinguish terminal agent failures from infra casualties.
    num_turns: int | None = None
    result_subtype: str | None = None
    duration_api_ms: int | None = None
    # The tool surface actually offered to the agent this trial, parsed
    # from the stream-json init event (codeprobe-9p6). None when the
    # adapter has no streaming transcript to parse; a captured-but-empty
    # manifest is represented as McpInitManifest(captured=...) so a failed
    # attach is never silently indistinguishable from a declined tool.
    mcp_init: McpInitManifest | None = None

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
    """Configuration passed to an agent adapter.

    ``allowed_tools`` / ``disallowed_tools`` restrict which tools the agent
    may call. When both are ``None`` the adapter uses its default tool set.
    When ``allowed_tools`` is an empty list, the adapter disables all
    built-in tools (useful for MCP-only experiments: MCP tools are still
    available because they come from ``mcp_config``, but no built-in
    ``Read``/``Grep``/``Bash``/etc. are).

    ``max_turns`` caps the number of agent turns per task. ``None`` means
    uncapped (the historical default — codeprobe relied on the per-task
    subprocess timeout alone). Reference rigs cap aggressively: CSB at 30
    turns, EB at 50 turns. See ``docs/agent_config.md`` for guidance.
    """

    model: str | None = None
    permission_mode: str = "default"
    timeout_seconds: int = 3600
    mcp_config: dict | None = None
    allowed_tools: list[str] | None = None
    disallowed_tools: list[str] | None = None
    extra: dict | None = None
    cwd: str | None = None
    max_turns: int | None = None


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

    @property
    def capabilities(self) -> AdapterCapabilities:
        """Which :class:`AgentConfig` knobs this adapter honors.

        FAIL-CLOSED: run preflight treats a missing declaration as
        "prompt+model only" and refuses any arm requesting more (use
        :func:`capabilities_of` to read it safely).
        """
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
