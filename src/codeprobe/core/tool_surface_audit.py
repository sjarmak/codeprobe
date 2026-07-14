"""Tool-surface utilization audit (codeprobe-1gg).

The 9tk Sourcegraph confirm produced its key METHODOLOGICAL finding by
accident: ``with-sg-narrow`` looked equivalent to ``local-only`` not
because narrowing the surface is neutral, but because the agent declined
the read-blocked surface and made ZERO Sourcegraph calls. That arm's
"tooling effect" was really "the agent ignored the tooling" — an INVALID
comparison masquerading as a null result. It took a manual validity audit
to catch.

This module turns that manual check into a mechanism. The raw signal
already exists end-to-end: ``ExperimentConfig`` declares the enabled tool
surface (``mcp_config`` / ``allowed_tools``) and ``CompletedTask`` carries
``tool_use_by_name``. The audit is a pure set intersection — declared
surface vs used tools — so it is ZFC-compliant: no semantic judgment, only
mechanical counting.

The surface policies are DERIVED from the config, never hardcoded. A lint
(``tests/lint/test_tool_surface_policy.py``) forbids constructing a
:class:`ToolSurfacePolicy` from string literals so the surface vocabulary
can never drift away from what the experiment actually declared.

Honesty boundary (bead A2): a surface with zero calls is only flagged as
*abandoned* when the trial actually ran. Infra casualties (quota stubs,
crashes) and trials whose tool usage was never captured are reported with
an explicit non-abandonment reason instead — "the agent declined the tool"
must never be conflated with "infra made the tool unavailable".
"""

from __future__ import annotations

from dataclasses import dataclass

from codeprobe.models.experiment import CompletedTask, ExperimentConfig

# Canonical MCP tool-name prefix. The Claude CLI exposes a server's tools
# as ``mcp__<server>__<tool>`` (see core/mcp_policy._mcp_server_allowlist),
# so a server named ``sourcegraph`` owns every tool under ``mcp__sourcegraph``.
_MCP_PREFIX = "mcp__"


@dataclass(frozen=True)
class ToolSurfacePolicy:
    """A named tool surface and the tool-name prefixes that belong to it.

    Constructed only from values derived from an :class:`ExperimentConfig`
    (see :func:`derive_surface_policies`). The lint forbids building one
    from string literals so policies stay config-driven.
    """

    surface: str
    prefixes: tuple[str, ...]

    def matches(self, tool_name: str) -> bool:
        return any(tool_name.startswith(p) for p in self.prefixes)


@dataclass(frozen=True)
class SurfaceAuditFinding:
    """Per-trial verdict for one enabled surface.

    ``abandoned`` is the load-bearing field: True only when the surface was
    enabled, the trial ran, usage was captured, and the agent made zero
    calls. ``reason`` records why a zero-call surface was NOT flagged so the
    distinction in bead A2 is auditable.
    """

    surface: str
    enabled: bool
    calls: int
    abandoned: bool
    reason: str


def _server_names_from_mcp_config(mcp_config: dict | None) -> list[str]:
    """Return the MCP server names declared in *mcp_config*."""
    if not isinstance(mcp_config, dict):
        return []
    servers = mcp_config.get("mcpServers", {})
    if not isinstance(servers, dict):
        return []
    return [str(name) for name in servers]


def _server_names_from_allowed_tools(allowed_tools: list[str] | None) -> list[str]:
    """Extract MCP server names from any ``mcp__<server>__*`` allowlist entries.

    A config can enable a surface purely through ``allowed_tools`` without a
    separate ``mcp_config`` block; this recovers the server name from the
    canonical ``mcp__<server>__<tool>`` shape.
    """
    if not allowed_tools:
        return []
    names: list[str] = []
    for tool in allowed_tools:
        if not isinstance(tool, str) or not tool.startswith(_MCP_PREFIX):
            continue
        remainder = tool[len(_MCP_PREFIX) :]
        server = remainder.split("__", 1)[0]
        if server:
            names.append(server)
    return names


def derive_surface_policies(config: ExperimentConfig) -> list[ToolSurfacePolicy]:
    """Derive the enabled tool surfaces from an experiment config.

    The surface vocabulary comes entirely from the config: each declared
    MCP server (from ``mcp_config.mcpServers`` and any ``mcp__<server>__*``
    entries in ``allowed_tools``) becomes a surface named after the server,
    owning every tool under its ``mcp__<server>`` prefix. Returns an empty
    list for configs that enable no MCP surface (e.g. a local-only arm),
    which correctly produces zero abandonment findings.
    """
    names: list[str] = []
    seen: set[str] = set()
    for name in (
        *_server_names_from_mcp_config(config.mcp_config),
        *_server_names_from_allowed_tools(config.allowed_tools),
    ):
        if name not in seen:
            seen.add(name)
            names.append(name)
    return [
        ToolSurfacePolicy(surface=name, prefixes=(f"{_MCP_PREFIX}{name}",))
        for name in names
    ]


def _is_infra_failure(task: CompletedTask) -> bool:
    """True when the trial was an infra casualty, not a real measurement.

    Infra casualties (status ``error``, or a quota stub) mean the agent
    never got a fair shot at the surface, so a zero-call surface must NOT
    be charged as abandonment (bead A2).

    Deliberately narrower than ``analysis.validity.is_infra_failure``
    (codeprobe-77z): this predicate answers "did the agent get a fair shot at
    the surface" — a terminal ``error_max_turns`` trial DID get its turns, so a
    zero-call surface there is a real abandonment signal — whereas the validity
    gate answers "should this 0.0 leave the reward population and force a
    re-run". The two questions overlap but are not the same, so this stays
    local rather than importing the gate predicate (which would also close a
    core→analysis import cycle; see the deferred-import note in
    ``analysis/stats.py``).
    """
    return task.status == "error" or task.error_category == "quota"


def audit_tool_surface_usage(
    task: CompletedTask, config: ExperimentConfig
) -> list[SurfaceAuditFinding]:
    """Audit one trial's use of every surface its config enabled.

    Emits one finding per enabled surface. A finding is ``abandoned`` only
    when the surface was enabled, the trial ran (not an infra casualty),
    tool usage was captured, and the agent made zero matching calls.
    """
    policies = derive_surface_policies(config)
    if not policies:
        return []

    usage = task.tool_use_by_name
    infra_failed = _is_infra_failure(task)

    findings: list[SurfaceAuditFinding] = []
    for policy in policies:
        if usage is None:
            # Usage was never captured (no streaming transcript). We cannot
            # prove zero calls, so we never assert abandonment.
            findings.append(
                SurfaceAuditFinding(
                    surface=policy.surface,
                    enabled=True,
                    calls=0,
                    abandoned=False,
                    reason="usage-not-captured",
                )
            )
            continue

        calls = sum(
            count for name, count in usage.items() if policy.matches(name)
        )
        if calls > 0:
            reason = "used"
        elif infra_failed:
            reason = "infra-failure"
        else:
            reason = "zero-calls"
        findings.append(
            SurfaceAuditFinding(
                surface=policy.surface,
                enabled=True,
                calls=calls,
                abandoned=(calls == 0 and not infra_failed),
                reason=reason,
            )
        )
    return findings


def task_abandoned_any_surface(
    task: CompletedTask, config: ExperimentConfig
) -> bool:
    """True when the trial abandoned at least one enabled surface."""
    return any(f.abandoned for f in audit_tool_surface_usage(task, config))
