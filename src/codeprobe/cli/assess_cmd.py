"""codeprobe assess — evaluate a codebase's benchmarking potential."""

from __future__ import annotations

import click


def run_assess(path: str) -> None:
    """Assess a codebase for AI agent benchmarking potential."""
    from pathlib import Path

    from codeprobe.assess import assess_repo

    repo_path = Path(path).resolve()
    if not repo_path.is_dir():
        click.echo(f"Error: {repo_path} is not a directory.", err=True)
        raise SystemExit(1)
    if not (repo_path / ".git").exists():
        click.echo(f"Error: {repo_path} does not appear to be a git repository.", err=True)
        raise SystemExit(1)
    score = assess_repo(repo_path)

    click.echo(f"Codebase Assessment: {repo_path.name}")
    click.echo(f"{'=' * 50}")
    click.echo()
    click.echo(f"Overall Score: {score.overall:.0%}")
    click.echo()
    click.echo("Breakdown:")
    click.echo(f"  Task Richness:  {score.task_richness:.0%}")
    click.echo(f"  Test Coverage:  {score.test_coverage:.0%}")
    click.echo(f"  Complexity:     {score.complexity:.0%}")
    click.echo(f"  Activity:       {score.activity:.0%}")
    click.echo()
    click.echo(f"Recommendation: {score.recommendation}")

    if score.overall >= 0.5:
        click.echo()
        click.echo("Next: codeprobe mine . --count 5")
