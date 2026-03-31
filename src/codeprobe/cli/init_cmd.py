"""codeprobe init — interactive setup wizard."""

from __future__ import annotations

import click


def run_init(path: str) -> None:
    """Interactive wizard: What do you want to learn?"""
    click.echo("Welcome to codeprobe!")
    click.echo()
    click.echo("What do you want to learn?")
    click.echo()
    click.echo("  1. Compare baseline agent vs MCP-augmented agent")
    click.echo("  2. Compare different models (e.g., Sonnet vs Opus)")
    click.echo("  3. Compare different prompts or instruction styles")
    click.echo("  4. Custom comparison")
    click.echo()

    goal = click.prompt("Choose a goal", type=click.IntRange(1, 4), default=1)

    # TODO: Wire to experiment setup from MCP-Eval-Tasks
    click.echo()
    click.echo(f"Setting up experiment in {path}...")
    click.echo()
    click.echo("Next steps:")
    click.echo(f"  codeprobe mine {path}     # Mine tasks from your repo")
    click.echo(f"  codeprobe run {path}      # Run agents against tasks")
    click.echo(f"  codeprobe interpret {path} # Analyze results")
