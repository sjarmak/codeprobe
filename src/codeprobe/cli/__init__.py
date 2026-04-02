"""CLI entry point for codeprobe."""

import click

from codeprobe import __version__


@click.group()
@click.version_option(version=__version__, prog_name="codeprobe")
def main() -> None:
    """Benchmark AI coding agents against your own codebase.

    Mine real tasks from your repo history, run agents against them,
    and interpret the results to find which setup works best for YOUR code.
    """


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
def init(path: str) -> None:
    """Interactive setup wizard — what do you want to learn?

    Walks you through choosing what to compare (models, tools, prompts),
    mining tasks from your repo, and configuring your first experiment.
    """
    from codeprobe.cli.init_cmd import run_init

    run_init(path)


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--count", default=5, help="Number of tasks to mine (3-20).")
@click.option(
    "--source",
    default="auto",
    help="Git host: github, gitlab, bitbucket, azure, gitea, local, auto.",
)
@click.option(
    "--min-files",
    default=0,
    type=int,
    help="Minimum changed files per task. Use 4+ to bias toward harder tasks.",
)
@click.option(
    "--subsystem",
    multiple=True,
    default=(),
    help="Filter to subsystem prefixes. Repeatable: --subsystem pkg/ --subsystem cmd/",
)
@click.option(
    "--discover-subsystems",
    is_flag=True,
    default=False,
    help="List subsystems from merge history and pick interactively.",
)
def mine(
    path: str,
    count: int,
    source: str,
    min_files: int,
    subsystem: tuple[str, ...],
    discover_subsystems: bool,
) -> None:
    """Mine eval tasks from a repository's history.

    Extracts real code-change tasks from merged PRs/MRs with ground truth,
    test scripts, and scoring rubrics.
    """
    from codeprobe.cli.mine_cmd import run_mine

    run_mine(
        path,
        count=count,
        source=source,
        min_files=min_files,
        subsystems=subsystem,
        discover_subsystems=discover_subsystems,
    )


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--agent", default="claude", help="Agent to evaluate: claude, copilot.")
@click.option("--model", default=None, help="Model override (e.g., claude-sonnet-4-6).")
@click.option(
    "--config", default=None, help="Path to .evalrc.yaml or experiment directory."
)
@click.option(
    "--max-cost-usd",
    default=None,
    type=float,
    envvar="CODEPROBE_MAX_COST_USD",
    help="Maximum cumulative cost in USD before halting. Env: CODEPROBE_MAX_COST_USD.",
)
@click.option(
    "--parallel",
    default=5,
    type=int,
    envvar="CODEPROBE_PARALLEL",
    help="Max concurrent task executions per config (default: 5). Env: CODEPROBE_PARALLEL.",
)
def run(
    path: str,
    agent: str,
    model: str | None,
    config: str | None,
    max_cost_usd: float | None,
    parallel: int,
) -> None:
    """Run eval tasks against an AI coding agent.

    Spawns isolated agent sessions for each task, scores results with
    automated tests, and produces a results summary.
    """
    from codeprobe.cli.run_cmd import run_eval

    run_eval(
        path,
        agent=agent,
        model=model,
        config=config,
        max_cost_usd=max_cost_usd,
        parallel=parallel,
    )


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option(
    "--format", "fmt", default="text", help="Output format: text, json, html."
)
def interpret(path: str, fmt: str) -> None:
    """Analyze eval results and get recommendations.

    Compares configurations statistically, ranks by score and cost-efficiency,
    and produces actionable recommendations.
    """
    from codeprobe.cli.interpret_cmd import run_interpret

    run_interpret(path, fmt=fmt)


@main.group()
def experiment() -> None:
    """Manage eval experiments — init, configure, validate, and aggregate."""


@experiment.command("init")
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--name", required=True, help="Experiment name (used as directory name).")
@click.option("--description", default="", help="One-line experiment description.")
def init_experiment(path: str, name: str, description: str) -> None:
    """Create a new experiment directory."""
    from codeprobe.cli.experiment_cmd import experiment_init

    experiment_init(path, name=name, description=description)


# Use a hyphenated command name to match the reference CLI
@experiment.command("add-config")
@click.argument("path", type=click.Path(exists=True))
@click.option("--label", required=True, help="Human-readable config label.")
@click.option("--agent", default="claude", help="Agent backend (claude, copilot).")
@click.option("--model", default=None, help="Model ID (e.g., claude-sonnet-4-6).")
@click.option("--permission-mode", default="default", help="Permission mode for agent.")
@click.option(
    "--mcp-config", default=None, help="MCP config as JSON string or file path."
)
def add_config(
    path: str,
    label: str,
    agent: str,
    model: str | None,
    permission_mode: str,
    mcp_config: str | None,
) -> None:
    """Add a configuration to an existing experiment."""
    from codeprobe.cli.experiment_cmd import experiment_add_config

    experiment_add_config(
        path,
        label=label,
        agent=agent,
        model=model,
        permission_mode=permission_mode,
        mcp_config_str=mcp_config,
    )


@experiment.command("validate")
@click.argument("path", type=click.Path(exists=True))
def validate_experiment(path: str) -> None:
    """Validate experiment structure and readiness."""
    from codeprobe.cli.experiment_cmd import experiment_validate

    experiment_validate(path)


@experiment.command("status")
@click.argument("path", type=click.Path(exists=True))
def status_experiment(path: str) -> None:
    """Report completion status per configuration."""
    from codeprobe.cli.experiment_cmd import experiment_status

    experiment_status(path)


@experiment.command("aggregate")
@click.argument("path", type=click.Path(exists=True))
def aggregate_experiment(path: str) -> None:
    """Aggregate results across configurations into a comparison report."""
    from codeprobe.cli.experiment_cmd import experiment_aggregate

    experiment_aggregate(path)


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
def assess(path: str) -> None:
    """Assess a codebase for AI agent benchmarking potential.

    Analyzes repo structure, complexity, and history to estimate
    how well-suited it is for meaningful agent evaluation.
    """
    from codeprobe.cli.assess_cmd import run_assess

    run_assess(path)


# Register the ratings subcommand group
from codeprobe.cli.ratings_cmd import ratings  # noqa: E402

main.add_command(ratings)

# Register the scaffold subcommand group
from codeprobe.cli.scaffold_cmd import scaffold  # noqa: E402

main.add_command(scaffold)

# Register the probe command
from codeprobe.cli.probe_cmd import probe  # noqa: E402

main.add_command(probe)
