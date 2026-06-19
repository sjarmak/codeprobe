"""``codeprobe models`` subcommand group — list the model tokens each agent
accepts.

The known-model registry (``codeprobe.adapters.models``) is the single source
of truth shared with run-time ``--model`` validation and the ``init`` wizard,
so what this command prints is exactly what ``codeprobe run`` will accept.

Attached to the root CLI from ``src/codeprobe/cli/__init__.py``
(``main.add_command(models)``).
"""

from __future__ import annotations

import click

from codeprobe.adapters.models import known_agents, model_set

__all__ = ["models"]


@click.group()
def models() -> None:
    """Inspect the model tokens codeprobe accepts per agent."""


@models.command("list")
@click.option(
    "--agent",
    "agent",
    default=None,
    help="Show models for a single agent (claude, codex, copilot). "
    "Omit to list all.",
)
def list_models(agent: str | None) -> None:
    """List the known model tokens (aliases + canonical ids) per agent."""
    agents = [agent] if agent else known_agents()
    for name in agents:
        ms = model_set(name)
        if ms is None:
            raise click.BadParameter(
                f"Unknown agent {name!r}. Known agents: "
                f"{', '.join(known_agents())}.",
                param_hint="--agent",
            )
        suffix = "" if ms.validated else "  (advisory — not enforced)"
        click.echo(f"{name}{suffix}")
        if ms.default:
            click.echo(f"  default: {ms.default}")
        if ms.aliases:
            for alias, canonical in ms.aliases.items():
                click.echo(f"  {alias}  ->  {canonical}")
        for full_id in ms.full_ids:
            if full_id not in ms.aliases.values():
                click.echo(f"  {full_id}")
        if not ms.known_tokens():
            click.echo("  (agent selects its own model; no fixed token list)")
        click.echo()
