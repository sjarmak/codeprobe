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
@click.option("--source", default="auto", help="Git host: github, gitlab, bitbucket, azure, gitea, local, auto.")
def mine(path: str, count: int, source: str) -> None:
    """Mine eval tasks from a repository's history.

    Extracts real code-change tasks from merged PRs/MRs with ground truth,
    test scripts, and scoring rubrics.
    """
    from codeprobe.cli.mine_cmd import run_mine

    run_mine(path, count=count, source=source)


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--agent", default="claude", help="Agent to evaluate: claude, copilot.")
@click.option("--model", default=None, help="Model override (e.g., claude-sonnet-4-6).")
@click.option("--config", default=None, help="Path to .evalrc.yaml or experiment directory.")
@click.option(
    "--max-cost-usd",
    default=None,
    type=float,
    envvar="CODEPROBE_MAX_COST_USD",
    help="Maximum cumulative cost in USD before halting. Env: CODEPROBE_MAX_COST_USD.",
)
def run(path: str, agent: str, model: str | None, config: str | None, max_cost_usd: float | None) -> None:
    """Run eval tasks against an AI coding agent.

    Spawns isolated agent sessions for each task, scores results with
    automated tests, and produces a results summary.
    """
    from codeprobe.cli.run_cmd import run_eval

    run_eval(path, agent=agent, model=model, config=config, max_cost_usd=max_cost_usd)


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--format", "fmt", default="text", help="Output format: text, json, html.")
def interpret(path: str, fmt: str) -> None:
    """Analyze eval results and get recommendations.

    Compares configurations statistically, ranks by score and cost-efficiency,
    and produces actionable recommendations.
    """
    from codeprobe.cli.interpret_cmd import run_interpret

    run_interpret(path, fmt=fmt)


@main.command()
@click.argument("path", default=".", type=click.Path(exists=True))
def assess(path: str) -> None:
    """Assess a codebase for AI agent benchmarking potential.

    Analyzes repo structure, complexity, and history to estimate
    how well-suited it is for meaningful agent evaluation.
    """
    from codeprobe.cli.assess_cmd import run_assess

    run_assess(path)
