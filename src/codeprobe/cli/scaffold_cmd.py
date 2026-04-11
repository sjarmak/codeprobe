"""codeprobe scaffold — create and validate eval task directories."""

from __future__ import annotations

from pathlib import Path

import click


@click.group()
def scaffold() -> None:
    """Scaffold eval task directories.

    Create new task directories with the standard layout,
    or validate existing ones.
    """


@scaffold.command()
@click.option("--id", "task_id", required=True, help="Unique task identifier.")
@click.option("--repo", required=True, help="Repository (e.g., org/repo).")
@click.option(
    "--output", "output_dir", default=".", type=click.Path(), help="Output directory."
)
@click.option("--instruction", default="", help="Task instruction text.")
@click.option("--description", default="", help="Short task description.")
@click.option("--difficulty", default="medium", help="Difficulty: easy, medium, hard.")
@click.option("--category", default="sdlc", help="Task category.")
@click.option(
    "--time-limit",
    "time_limit_sec",
    default=300,
    type=int,
    help="Time limit in seconds.",
)
@click.option(
    "--reward-type",
    default="binary",
    help="Reward type: binary, test_ratio, exact_match.",
)
def task(
    task_id: str,
    repo: str,
    output_dir: str,
    instruction: str,
    description: str,
    difficulty: str,
    category: str,
    time_limit_sec: int,
    reward_type: str,
) -> None:
    """Create a new eval task directory.

    Generates instruction.md, task.toml, and tests/test.sh
    in the standard codeprobe layout.
    """
    from codeprobe.scaffold.writer import TaskSpec, write_task_dir

    spec = TaskSpec(
        task_id=task_id,
        repo=repo,
        instruction=instruction,
        description=description,
        difficulty=difficulty,
        category=category,
        time_limit_sec=time_limit_sec,
        reward_type=reward_type,
    )

    try:
        result_dir = write_task_dir(spec, Path(output_dir))
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    click.echo(f"Created task: {result_dir}")


@scaffold.command("upgrade-to-dual")
@click.argument("path", type=click.Path(exists=True))
@click.option(
    "--repo-path",
    required=True,
    type=click.Path(exists=True),
    help="Path to the git repository.",
)
def upgrade_to_dual(path: str, repo_path: str) -> None:
    """Upgrade a test_script task to dual verification mode.

    Reads metadata.json, extracts changed files from the ground_truth_commit,
    writes tests/ground_truth.json, and updates verification_mode to 'dual'.
    Skips if already dual.
    """
    from codeprobe.scaffold.writer import upgrade_to_dual as _upgrade

    try:
        result_dir = _upgrade(Path(path), Path(repo_path))
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)

    click.echo(f"Upgraded: {result_dir}")


@scaffold.command()
@click.argument("path", type=click.Path(exists=True))
def validate(path: str) -> None:
    """Validate an existing task directory structure.

    Checks for required files (instruction.md, task.toml, tests/test.sh)
    and correct permissions.
    """
    from codeprobe.scaffold.writer import validate_task_dir

    errors = validate_task_dir(Path(path))

    if not errors:
        click.echo(f"Valid: {path}")
        return

    for err in errors:
        click.echo(f"  [{err.severity.upper()}] {err.message}", err=True)
    raise SystemExit(1)
