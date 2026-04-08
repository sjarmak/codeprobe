"""codeprobe init — interactive setup wizard."""

from __future__ import annotations

import json
from pathlib import Path

import click

from codeprobe.cli.wizard import (
    ask_custom,
    ask_mcp_comparison,
    ask_model_comparison,
    ask_prompt_comparison,
    validate_experiment_name,
)
from codeprobe.core.experiment import create_experiment_dir
from codeprobe.core.registry import available
from codeprobe.models.evalrc import EvalrcConfig
from codeprobe.models.experiment import Experiment, ExperimentConfig

_GOAL_DEFAULTS = {
    1: "mcp-comparison",
    2: "model-comparison",
    3: "prompt-comparison",
    4: "custom",
}


def run_init(path: str) -> None:
    """Interactive wizard: What do you want to learn?"""
    target = Path(path).resolve()
    agents = available()
    if not agents:
        raise click.ClickException(
            "No agent adapters registered. Install an adapter first."
        )

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

    default_name = _GOAL_DEFAULTS[goal]
    experiment_name = click.prompt("Experiment name", default=default_name)
    validate_experiment_name(experiment_name)

    if goal == 1:
        evalrc, configs = _goal_mcp(agents, experiment_name)
    elif goal == 2:
        evalrc, configs = _goal_models(agents, experiment_name)
    elif goal == 3:
        evalrc, configs = _goal_prompts(agents, experiment_name)
    else:
        evalrc, configs = _goal_custom(agents, experiment_name)

    # Create experiment directory
    experiment = Experiment(
        name=experiment_name,
        description=evalrc.description,
        configs=configs,
    )
    codeprobe_dir = target / ".codeprobe"
    codeprobe_dir.mkdir(exist_ok=True)
    exp_dir = create_experiment_dir(codeprobe_dir, experiment)

    # Summary
    click.echo()
    click.echo(f"Created {exp_dir.relative_to(target)}/")
    click.echo(f"  Configurations: {', '.join(c.label for c in configs)}")
    click.echo()
    click.echo("Next steps:")
    click.echo(f"  codeprobe mine {path}      # Mine tasks from your repo")
    click.echo(f"  codeprobe run {path}       # Run agents against tasks")
    click.echo(f"  codeprobe interpret {path}  # Analyze results")


def _prompt_agent(agents: list[str]) -> str:
    """Prompt for agent selection."""
    agents_str = ", ".join(agents)
    result: str = click.prompt(f"Agent ({agents_str})", default=agents[0])
    return result


def _prompt_model() -> str | None:
    """Prompt for optional model override."""
    model = click.prompt(
        "Model (optional, press Enter to skip)", default="", show_default=False
    )
    return model if model else None


_Result = tuple[EvalrcConfig, list[ExperimentConfig]]

# Known locations for MCP config files, searched in order.
_MCP_SEARCH_PATHS = [
    Path.home() / ".claude" / ".mcp.json",
    Path.home() / ".claude" / "mcp-configs" / "mcp-servers.json",
    Path.home() / ".claude" / "settings.json",
    Path.home() / ".claude" / "settings.local.json",
]


def _discover_mcp_configs() -> list[tuple[Path, list[str]]]:
    """Scan known locations for MCP config files with mcpServers keys.

    Returns a list of (path, server_names) for each file that has servers.
    """
    found: list[tuple[Path, list[str]]] = []
    for p in _MCP_SEARCH_PATHS:
        if not p.is_file():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        servers = data.get("mcpServers", {})
        if servers:
            found.append((p, sorted(servers.keys())))
    # Also check .mcp.json in the current directory
    local_mcp = Path.cwd() / ".mcp.json"
    if local_mcp.is_file():
        try:
            data = json.loads(local_mcp.read_text(encoding="utf-8"))
            servers = data.get("mcpServers", {})
            if servers:
                found.append((local_mcp, sorted(servers.keys())))
        except (json.JSONDecodeError, OSError):
            pass
    return found


def _prompt_mcp_config() -> str:
    """Prompt for MCP config with auto-discovery of known locations."""
    discovered = _discover_mcp_configs()

    if discovered:
        click.echo()
        click.echo("Discovered MCP configurations:")
        for i, (p, servers) in enumerate(discovered, 1):
            click.echo(f"  {i}. {p}  ({len(servers)} servers)")
            for s in servers:
                click.echo(f"     - {s}")
        click.echo(f"  {len(discovered) + 1}. Enter a custom path")
        click.echo()

        choice = click.prompt(
            "Select MCP config",
            type=click.IntRange(1, len(discovered) + 1),
            default=1,
        )
        if choice <= len(discovered):
            return str(discovered[choice - 1][0])

    # Manual entry with tilde expansion
    while True:
        raw = click.prompt("Path to MCP config JSON")
        expanded = Path(raw).expanduser().resolve()
        if expanded.is_file():
            return str(expanded)
        click.echo(f"  Error: '{expanded}' does not exist. Try again.")


def _detect_sourcegraph_in_mcp(
    discovered: list[tuple[Path, list[str]]],
    mcp_data: dict | None = None,
) -> bool:
    """Return True if any discovered MCP config contains a Sourcegraph server.

    Checks server names for common Sourcegraph patterns (e.g.
    ``sourcegraph``, ``sourcegraph-mcp-server``).
    """
    sg_names = {"sourcegraph", "sourcegraph-mcp-server"}
    for _path, server_names in discovered:
        for name in server_names:
            if name.lower() in sg_names:
                return True
    if mcp_data:
        for name in mcp_data.get("mcpServers", {}):
            if name.lower() in sg_names:
                return True
    return False


def _prompt_sourcegraph_token() -> str:
    """Prompt for Sourcegraph access token, checking env var first."""
    import os

    env_token = os.environ.get("SOURCEGRAPH_TOKEN", "")
    if env_token:
        masked = env_token[:4] + "..." + env_token[-4:] if len(env_token) > 8 else "***"
        click.echo(f"  Found SOURCEGRAPH_TOKEN in environment ({masked})")
        if click.confirm("  Use this token?", default=True):
            return env_token

    return click.prompt("Sourcegraph access token")


def _prompt_sourcegraph_url() -> str | None:
    """Prompt for optional custom Sourcegraph instance URL."""
    url = click.prompt(
        "Sourcegraph URL (press Enter for sourcegraph.com)",
        default="",
        show_default=False,
    )
    return url if url else None


def _goal_mcp(agents: list[str], name: str) -> _Result:
    """Goal 1: MCP comparison prompts."""
    agent = _prompt_agent(agents)
    model = _prompt_model()

    # Check if Sourcegraph is available in discovered MCP configs
    discovered = _discover_mcp_configs()
    use_sourcegraph = False

    if _detect_sourcegraph_in_mcp(discovered):
        click.echo()
        click.echo("Detected Sourcegraph MCP server in your configuration.")
        click.echo("codeprobe can use the HTTP endpoint for better performance.")
        use_sourcegraph = click.confirm("Use Sourcegraph HTTP MCP?", default=True)
    else:
        click.echo()
        click.echo("Would you like to use Sourcegraph as the MCP server?")
        use_sourcegraph = click.confirm("Use Sourcegraph?", default=False)

    if use_sourcegraph:
        token = _prompt_sourcegraph_token()
        sg_url = _prompt_sourcegraph_url()
        return ask_mcp_comparison(
            experiment_name=name,
            agent=agent,
            model=model,
            sourcegraph_token=token,
            sourcegraph_url=sg_url,
        )

    # Fall back to generic MCP config path
    mcp_path = _prompt_mcp_config()
    return ask_mcp_comparison(
        experiment_name=name,
        agent=agent,
        model=model,
        mcp_config_path=mcp_path,
    )


def _goal_models(agents: list[str], name: str) -> _Result:
    """Goal 2: Model comparison prompts."""
    agent = _prompt_agent(agents)
    models_raw = click.prompt("Models to compare (comma-separated)")
    models = [m.strip() for m in models_raw.split(",") if m.strip()]
    if not models:
        raise click.BadParameter("At least one model is required.")

    return ask_model_comparison(
        experiment_name=name,
        agent=agent,
        models=models,
    )


def _goal_prompts(agents: list[str], name: str) -> _Result:
    """Goal 3: Prompt comparison prompts."""
    agent = _prompt_agent(agents)
    model = _prompt_model()
    variants_raw = click.prompt("Instruction variant paths (comma-separated)")
    variants = [v.strip() for v in variants_raw.split(",") if v.strip()]
    if not variants:
        raise click.BadParameter("At least one instruction variant path is required.")

    return ask_prompt_comparison(
        experiment_name=name,
        agent=agent,
        model=model,
        variants=variants,
    )


def _goal_custom(agents: list[str], name: str) -> _Result:
    """Goal 4: Custom comparison prompts."""
    count = click.prompt(
        "Number of configurations", type=click.IntRange(2, 10), default=2
    )

    configs_input: list[dict] = []
    for i in range(1, count + 1):
        click.echo(f"\n--- Configuration {i} ---")
        label = click.prompt("Label")
        agent = _prompt_agent(agents)
        model = _prompt_model()
        entry: dict = {"label": label, "agent": agent}
        if model:
            entry["model"] = model
        configs_input.append(entry)

    return ask_custom(
        experiment_name=name,
        configs=configs_input,
    )
