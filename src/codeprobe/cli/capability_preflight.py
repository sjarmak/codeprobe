"""Pre-spend refusal of experiment arms whose knobs the adapter cannot honor.

Experiment knobs used to silently no-op on non-Claude adapters: an
"MCP-strict vs baseline" copilot A/B never blocked Grep/Bash/Glob/Read,
so the report labelled arms as different configs while the numbers
compared nothing (codeprobe-f7rl.26). This module checks each arm's
requested knobs against the adapter's declared
:class:`~codeprobe.adapters.protocol.AdapterCapabilities` — fail-closed,
before any adapter spawns — and hard-refuses on mismatch. There is no
override flag: a knob that cannot be enforced must not be compared.
"""

from __future__ import annotations

from codeprobe.adapters.protocol import capabilities_of
from codeprobe.cli.errors import PrescriptiveError
from codeprobe.core.mcp_policy import resolve_tool_policy
from codeprobe.models.experiment import ExperimentConfig


def check_arm_capabilities(
    exp_config: ExperimentConfig,
    adapter: object,
    *,
    cli_max_turns: int | None = None,
) -> None:
    """Raise ``ADAPTER_CAPABILITY`` when *exp_config* requests unsupported knobs.

    Requested knobs are computed exactly as dispatch will resolve them:

    * ``mcp_config`` — set on the config.
    * ``allowed_tools`` / ``disallowed_tools`` — the
      :func:`resolve_tool_policy` output, which includes the
      strict/pragmatic auto-derived restrictions that silently no-op on
      adapters without tool-surface control.
    * ``max_turns`` — explicit field, legacy ``extra`` dict, or the CLI
      ``--max-turns`` flag (mirrors ``_run_config`` resolution).
    * ``permission_mode`` — the arm's DECLARED mode. The sandbox flip
      (``default`` → ``dangerously_skip``) happens later in
      ``_run_config``; an arm that never asked for a mode is not refused.

    The refusal is prescriptive and ``terminal`` — decision 4 of
    codeprobe-f7rl: no override flag.
    """
    caps = capabilities_of(adapter)
    unsupported: list[str] = []

    if exp_config.mcp_config and not caps.mcp_config:
        unsupported.append("mcp_config")

    policy = resolve_tool_policy(exp_config)
    if policy.allowed_tools is not None and not caps.allowed_tools:
        unsupported.append("allowed_tools")
    if policy.disallowed_tools is not None and not caps.disallowed_tools:
        unsupported.append("disallowed_tools")

    cfg_max_turns = (
        exp_config.max_turns
        if exp_config.max_turns is not None
        else exp_config.extra.get("max_turns")
    )
    resolved_max_turns = cli_max_turns if cli_max_turns is not None else cfg_max_turns
    if resolved_max_turns is not None and not caps.max_turns:
        unsupported.append("max_turns")

    if exp_config.permission_mode != "default" and not caps.permission_mode:
        unsupported.append("permission_mode")

    if not unsupported:
        return

    adapter_name = getattr(adapter, "name", type(adapter).__name__)
    knobs = ", ".join(unsupported)
    message = (
        f"Config {exp_config.label!r} requests knobs the {adapter_name!r} "
        f"adapter cannot honor: {knobs}. Running would silently drop them, "
        "so the arms would be labelled as different configs while comparing "
        "nothing. Use an adapter that supports these knobs (agent=claude) "
        "or remove them from the config."
    )
    tool_knobs_auto_derived = policy.mode in ("strict", "pragmatic") and (
        "allowed_tools" in unsupported or "disallowed_tools" in unsupported
    )
    if tool_knobs_auto_derived:
        message += (
            f" The tool restriction was auto-derived from "
            f"mcp_mode={policy.mode!r}; mcp_mode='loose' runs this arm "
            "without it, at the cost of a comparison-validity warning."
        )
    raise PrescriptiveError(
        code="ADAPTER_CAPABILITY",
        message=message,
        terminal=True,
        message_for_agent=(
            f"Set agent=claude for config {exp_config.label!r}: the "
            f"{adapter_name!r} adapter does not honor {knobs}."
        ),
        next_try_flag="--agent",
        next_try_value="claude",
        detail={
            "config_label": exp_config.label,
            "adapter": adapter_name,
            "unsupported_knobs": unsupported,
        },
    )
