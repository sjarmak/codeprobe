"""codeprobe init — interactive setup wizard."""

from __future__ import annotations

from pathlib import Path

import click

from codeprobe.cli.wizard import (
    ask_custom,
    ask_mcp_comparison,
    ask_model_comparison,
    ask_prompt_comparison,
    validate_experiment_name,
)
from codeprobe.cli.yaml_writer import write_evalrc
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
        raise click.ClickException("No agent adapters registered. Install an adapter first.")

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

    # Write .evalrc.yaml
    yaml_path = write_evalrc(target, evalrc)

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
    click.echo(f"Created {yaml_path.relative_to(target)}")
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
    return click.prompt(f"Agent ({agents_str})", default=agents[0])


def _prompt_model() -> str | None:
    """Prompt for optional model override."""
    model = click.prompt("Model (optional, press Enter to skip)", default="", show_default=False)
    return model if model else None


_Result = tuple[EvalrcConfig, list[ExperimentConfig]]


def _goal_mcp(agents: list[str], name: str) -> _Result:
    """Goal 1: MCP comparison prompts."""
    agent = _prompt_agent(agents)
    model = _prompt_model()
    mcp_path = click.prompt("Path to MCP config JSON", type=click.Path(exists=True))

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
    count = click.prompt("Number of configurations", type=click.IntRange(2, 10), default=2)

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
