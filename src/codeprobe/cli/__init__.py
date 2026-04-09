"""CLI entry point for codeprobe."""

import json as _json
import logging
import sys

import click

from codeprobe import __version__


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
        }
        return _json.dumps(payload)


def _configure_logging(verbose: int, quiet: bool, log_format: str = "text") -> None:
    """Configure namespace-scoped logging for codeprobe.* modules.

    Attaches a StreamHandler to `logging.getLogger("codeprobe")` so that
    all 26+ codeprobe.* modules emit through hierarchy without touching
    third-party loggers (httpx, urllib3, etc.).
    """
    if quiet:
        level = logging.WARNING
    elif verbose >= 1:
        level = logging.DEBUG
    else:
        level = logging.INFO

    logger = logging.getLogger("codeprobe")
    logger.setLevel(level)
    logger.propagate = False  # don't bubble to root

    # Idempotent: tests / repeat invocations must not duplicate handlers.
    for h in list(logger.handlers):
        logger.removeHandler(h)

    handler = logging.StreamHandler(sys.stderr)
    if log_format == "json":
        handler.setFormatter(_JsonFormatter())
    elif verbose >= 1:
        fmt = "%(levelname)s %(name)s: %(message)s"
        handler.setFormatter(logging.Formatter(fmt))
    else:
        fmt = "%(levelname)s: %(message)s"
        handler.setFormatter(logging.Formatter(fmt))
    logger.addHandler(handler)


@click.group()
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Increase log verbosity (-v sets DEBUG).",
)
@click.option(
    "-q",
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress INFO logs (WARNING and above only).",
)
@click.option(
    "--log-format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Log output format (default: text). 'json' emits one JSON object per line.",
)
@click.version_option(version=__version__, prog_name="codeprobe")
def main(verbose: int, quiet: bool, log_format: str) -> None:
    """Benchmark AI coding agents against your own codebase.

    Mine real tasks from your repo history, run agents against them,
    and interpret the results to find which setup works best for YOUR code.
    """
    _configure_logging(verbose=verbose, quiet=quiet, log_format=log_format)
    ctx = click.get_current_context()
    ctx.ensure_object(dict)
    ctx.obj["log_format"] = log_format
    ctx.obj["quiet"] = quiet


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
@click.argument("path", default=".")
@click.option(
    "--preset",
    type=click.Choice(["quick", "mcp"], case_sensitive=False),
    default=None,
    help="Apply a named preset: 'quick' (count=3) or 'mcp' (org-scale + MCP families).",
)
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
@click.option(
    "--enrich",
    is_flag=True,
    default=False,
    help="Enrich low-quality tasks via LLM (adds problem statement + acceptance criteria).",
)
@click.option(
    "--interactive/--no-interactive",
    default=None,
    help="Force interactive or non-interactive mode (default: auto-detect TTY).",
)
@click.option(
    "--no-llm",
    is_flag=True,
    default=False,
    help="Skip LLM instruction generation; use regex fallback (for offline/CI).",
)
@click.option(
    "--org-scale",
    is_flag=True,
    default=False,
    help="Mine org-scale comprehension tasks (oracle-verified) instead of SDLC tasks.",
)
@click.option(
    "--family",
    multiple=True,
    default=(),
    help="Limit org-scale mining to specific families. Repeatable.",
)
@click.option(
    "--repos",
    multiple=True,
    default=(),
    help="Repo paths or URLs for multi-repo org-scale mining. Repeatable.",
)
@click.option(
    "--scan-timeout",
    default=60,
    type=int,
    help="Per-family scan timeout in seconds (default: 60).",
)
@click.option(
    "--validate",
    "validate_flag",
    is_flag=True,
    default=False,
    help="Run MCP delta validation on mined families.",
)
@click.option(
    "--curate",
    is_flag=True,
    default=False,
    help="Enable curation pipeline (multi-backend ground truth with tiers).",
)
@click.option(
    "--backends",
    multiple=True,
    default=(),
    help="Curation backends to use: grep, sourcegraph, pr_diff, agent. Repeatable.",
)
@click.option(
    "--verify-curation",
    "verify_curation_flag",
    is_flag=True,
    default=False,
    help="Run LLM verification on curated ground truth.",
)
@click.option(
    "--mcp-families",
    is_flag=True,
    default=False,
    help="Include MCP-advantaged task families (symbol-reference-trace, "
    "type-hierarchy-consumers, change-scope-audit). Only with --org-scale.",
)
@click.option(
    "--sg-repo",
    default="",
    help="Sourcegraph repo identifier for ground truth enrichment "
    "(e.g. github.com/sg-evals/numpy). Defaults to github.com/sg-evals/{repo_name} "
    "when --mcp-families is used. Requires SOURCEGRAPH_TOKEN env var.",
)
def mine(
    path: str,
    preset: str | None,
    count: int,
    source: str,
    min_files: int,
    subsystem: tuple[str, ...],
    discover_subsystems: bool,
    enrich: bool,
    interactive: bool | None,
    no_llm: bool,
    org_scale: bool,
    family: tuple[str, ...],
    repos: tuple[str, ...],
    scan_timeout: int,
    validate_flag: bool,
    curate: bool,
    backends: tuple[str, ...],
    verify_curation_flag: bool,
    mcp_families: bool,
    sg_repo: str,
) -> None:
    """Mine eval tasks from a repository's history.

    Extracts real code-change tasks from merged PRs/MRs with ground truth,
    test scripts, and scoring rubrics.

    \b
    Presets (--preset):
      quick  — Fast scan: count=3, default SDLC mode
      mcp    — MCP eval: count=8, org-scale + MCP families + enrich

    \b
    Use --org-scale to mine comprehension/IR tasks with oracle verification
    instead of SDLC code-change tasks.

    By default, generates high-quality instructions via LLM (Haiku).
    Use --no-llm to skip LLM and fall back to regex/template extraction.

    When run interactively (default in a terminal), walks you through
    choosing an eval goal, task count, and git host before mining.
    Use --no-interactive to skip the prompts and use defaults/flags directly.
    """
    from codeprobe.cli.mine_cmd import run_mine

    run_mine(
        path,
        preset=preset,
        count=count,
        source=source,
        min_files=min_files,
        subsystems=subsystem,
        discover_subsystems=discover_subsystems,
        enrich=enrich,
        interactive=interactive,
        no_llm=no_llm,
        org_scale=org_scale,
        families=family,
        repos=repos,
        scan_timeout=scan_timeout,
        validate_flag=validate_flag,
        curate=curate,
        backends=backends,
        verify_curation_flag=verify_curation_flag,
        mcp_families=mcp_families,
        sg_repo=sg_repo,
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
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print estimated resource requirements without executing any agents.",
)
@click.pass_context
def run(
    ctx: click.Context,
    path: str,
    agent: str,
    model: str | None,
    config: str | None,
    max_cost_usd: float | None,
    parallel: int,
    dry_run: bool,
) -> None:
    """Run eval tasks against an AI coding agent.

    Spawns isolated agent sessions for each task, scores results with
    automated tests, and produces a results summary.
    """
    from codeprobe.cli.run_cmd import run_eval

    ctx.ensure_object(dict)
    log_format = ctx.obj.get("log_format", "text")
    quiet = ctx.obj.get("quiet", False)

    run_eval(
        path,
        agent=agent,
        model=model,
        config=config,
        max_cost_usd=max_cost_usd,
        parallel=parallel,
        dry_run=dry_run,
        log_format=log_format,
        quiet=quiet,
    )


@main.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--format", "fmt", default="text", help="Output format: text, json, csv.")
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
@click.option(
    "--instruction-variant",
    default=None,
    help="Instruction file variant (e.g., instruction_mcp.md). Default: instruction.md.",
)
@click.option(
    "--preamble",
    "preambles",
    multiple=True,
    help=(
        "Preamble to prepend to the instruction. Repeatable. "
        "Built-ins: sourcegraph, github. Or path to a custom .md file."
    ),
)
def add_config(
    path: str,
    label: str,
    agent: str,
    model: str | None,
    permission_mode: str,
    mcp_config: str | None,
    instruction_variant: str | None,
    preambles: tuple[str, ...],
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
        instruction_variant=instruction_variant,
        preambles=preambles,
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


@main.command("oracle-check")
@click.argument("task_dir", type=click.Path(exists=True))
@click.option(
    "--metric",
    default="f1",
    type=click.Choice(["f1", "recall", "precision", "jaccard", "weighted_f1"]),
    help="Primary scoring metric (default: f1).",
)
@click.option("--write-reward", is_flag=True, default=False, help="Write reward.txt.")
def oracle_check_cmd(task_dir: str, metric: str, write_reward: bool) -> None:
    """Compare agent answer against oracle ground truth.

    Reads answer.txt and ground_truth.json from TASK_DIR, computes F1/recall/
    precision/jaccard, and prints the result. Use --write-reward to write the
    score to reward.txt (for test.sh integration).
    """
    import json
    from pathlib import Path

    from codeprobe.mining.org_scale_oracle import oracle_check

    result = oracle_check(Path(task_dir), metric=metric)

    if result.get("error"):
        click.echo(f"Error: {result['error']}", err=True)

    click.echo(json.dumps(result, indent=2))

    if write_reward:
        reward_path = Path(task_dir) / "reward.txt"
        reward_path.write_text(str(result.get("score", 0.0)) + "\n")
        click.echo(f"Wrote {reward_path}")


# Register the ratings subcommand group
from codeprobe.cli.ratings_cmd import ratings  # noqa: E402

main.add_command(ratings)

# Register the scaffold subcommand group
from codeprobe.cli.scaffold_cmd import scaffold  # noqa: E402

main.add_command(scaffold)

# Register the probe command
from codeprobe.cli.probe_cmd import probe  # noqa: E402

main.add_command(probe)
