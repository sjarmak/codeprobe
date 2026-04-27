"""Tool-surface policy for MCP-augmented experiment configs.

When an :class:`ExperimentConfig` carries a non-empty ``mcp_config`` the
executor previously left the agent's full built-in tool surface (Grep,
Bash, Glob, Read, Write) alongside the MCP tools. This caused the
"with-mcp" arm of an A/B comparison to silently fall back to baseline
behavior whenever the model decided to skip MCP — yielding a tied
score band that did not actually measure MCP at all
(see bead ``codeprobe-p6vw`` for the gascity-mcp-comparison analysis).

This module resolves the effective ``allowed_tools`` /
``disallowed_tools`` lists so that an MCP arm measures what it claims
to measure. Three modes are supported via :class:`ExperimentConfig.mcp_mode`:

* ``"strict"`` (default): MCP servers + ``Write`` only. ``Grep``,
  ``Bash``, ``Glob`` and ``Read`` are blocked. The agent must use the
  MCP transport to investigate the repo.
* ``"pragmatic"``: MCP servers + ``Read`` + ``Write``. Local file
  reads stay available so the agent can verify MCP results, but
  full-text search and shell escapes are blocked.
* ``"loose"``: dual-surface — both built-ins and MCP tools available.
  Mirrors pre-0.9.0 behavior. Emits a runtime warning describing the
  validity trade-off.

Explicit ``allowed_tools`` on an :class:`ExperimentConfig` always wins:
a user who pins the surface keeps their pin regardless of ``mcp_mode``.
The same goes for explicit ``disallowed_tools``. Auto-restriction only
runs when neither field is set on the experiment config.
"""

from __future__ import annotations

from dataclasses import dataclass

from codeprobe.models.experiment import ExperimentConfig

ALLOWED_MCP_MODES = frozenset({"strict", "pragmatic", "loose"})
DEFAULT_MCP_MODE = "strict"

# Built-ins blocked by the strict / pragmatic policies.
_HARD_BLOCKED_BUILTINS = ("Grep", "Bash", "Glob")
# Strict additionally blocks local Read.
_STRICT_BLOCKED_BUILTINS = (*_HARD_BLOCKED_BUILTINS, "Read")

_LOOSE_WARNING = (
    "MCP tool-surface auto-restriction disabled (mcp_mode='loose'). "
    "The agent may freely choose between MCP tools and built-in "
    "Grep/Bash/Glob/Read — comparison validity is compromised because "
    "runs that skip MCP silently degenerate into baseline."
)


@dataclass(frozen=True)
class MCPToolPolicy:
    """Resolved tool restrictions for one experiment config.

    Attributes
    ----------
    allowed_tools:
        Effective whitelist passed to :class:`AgentConfig.allowed_tools`.
        ``None`` means "no whitelist" (adapter default).
    disallowed_tools:
        Effective blocklist passed to :class:`AgentConfig.disallowed_tools`.
        ``None`` means "no blocklist".
    mode:
        The mcp_mode actually applied (``"strict"``, ``"pragmatic"``,
        ``"loose"``, or ``"explicit"`` when the user pinned the surface).
    warning:
        Operator-facing warning string when the policy weakens validity
        (currently only ``loose``). ``None`` otherwise.
    """

    allowed_tools: list[str] | None
    disallowed_tools: list[str] | None
    mode: str
    warning: str | None = None


def _mcp_server_allowlist(mcp_config: dict) -> list[str]:
    """Return ``mcp__<server>`` entries for every MCP server in *mcp_config*.

    Claude CLI auto-approves at the server level when given the
    ``mcp__<server>`` prefix, so we don't need to enumerate every tool.
    Returns an empty list when *mcp_config* has no ``mcpServers``.
    """
    servers = mcp_config.get("mcpServers", {}) if isinstance(mcp_config, dict) else {}
    if not isinstance(servers, dict):
        return []
    return [f"mcp__{name}" for name in servers]


def resolve_tool_policy(exp_config: ExperimentConfig) -> MCPToolPolicy:
    """Compute the effective tool restrictions for *exp_config*.

    Resolution order:

    1. No ``mcp_config`` → return whatever the user set, no auto-policy.
    2. Explicit ``allowed_tools`` or ``disallowed_tools`` on the config →
       user wins (mode reported as ``"explicit"``).
    3. ``mcp_mode == "loose"`` → no auto-restriction, emit warning.
    4. ``mcp_mode == "pragmatic"`` → MCP servers + Read + Write,
       block Grep/Bash/Glob.
    5. ``mcp_mode == "strict"`` (default) → MCP servers + Write,
       block Grep/Bash/Glob/Read.
    """
    mode = exp_config.mcp_mode or DEFAULT_MCP_MODE
    if mode not in ALLOWED_MCP_MODES:
        raise ValueError(
            f"Invalid mcp_mode {mode!r} on config {exp_config.label!r}. "
            f"Allowed: {sorted(ALLOWED_MCP_MODES)}"
        )

    if not exp_config.mcp_config:
        return MCPToolPolicy(
            allowed_tools=exp_config.allowed_tools,
            disallowed_tools=exp_config.disallowed_tools,
            mode=mode,
        )

    if (
        exp_config.allowed_tools is not None
        or exp_config.disallowed_tools is not None
    ):
        return MCPToolPolicy(
            allowed_tools=exp_config.allowed_tools,
            disallowed_tools=exp_config.disallowed_tools,
            mode="explicit",
        )

    if mode == "loose":
        return MCPToolPolicy(
            allowed_tools=None,
            disallowed_tools=None,
            mode="loose",
            warning=_LOOSE_WARNING,
        )

    server_allow = _mcp_server_allowlist(exp_config.mcp_config)
    if mode == "pragmatic":
        allowed = [*server_allow, "Read", "Write"]
        blocked = list(_HARD_BLOCKED_BUILTINS)
    else:  # strict
        allowed = [*server_allow, "Write"]
        blocked = list(_STRICT_BLOCKED_BUILTINS)

    return MCPToolPolicy(
        allowed_tools=allowed,
        disallowed_tools=blocked,
        mode=mode,
    )
