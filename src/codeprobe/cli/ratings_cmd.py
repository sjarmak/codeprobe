"""codeprobe ratings — record and analyze agent session quality ratings."""

from __future__ import annotations

import csv
from pathlib import Path

import click

MIN_SAMPLE_SIZE = 15


@click.group()
def ratings() -> None:
    """Record and analyze agent session quality ratings.

    Collect micro-ratings after coding sessions, then summarize trends
    across models, MCPs, skills, and task types.
    """


@ratings.command()
@click.argument("rating", type=click.IntRange(1, 5))
@click.option(
    "--task-type", default="", help="Type of task (e.g., bugfix, feature, refactor)."
)
@click.option(
    "--duration", default=None, type=float, help="Session duration in seconds."
)
@click.option(
    "--tool-calls", default=None, type=int, help="Number of tool calls in the session."
)
@click.option("--path", default="ratings.jsonl", help="Path to ratings JSONL file.")
def record(
    rating: int,
    task_type: str,
    duration: float | None,
    tool_calls: int | None,
    path: str,
) -> None:
    """Record a session quality rating (1-5).

    RATING is an integer from 1 (poor) to 5 (excellent).
    """
    from codeprobe.ratings.collector import record_rating

    meta: dict[str, object] = {}
    if task_type:
        meta["task_type"] = task_type
    if duration is not None:
        meta["duration_s"] = duration
    if tool_calls is not None:
        meta["tool_calls"] = tool_calls

    rec = record_rating(
        rating,
        session_metadata=meta if meta else None,
        path=Path(path),
    )
    click.echo(f"Recorded rating={rec.rating} model={rec.config.model or '(auto)'}")


@ratings.command()
@click.option("--path", default="ratings.jsonl", help="Path to ratings JSONL file.")
def summary(path: str) -> None:
    """Print a summary of collected ratings."""
    from codeprobe.ratings.collector import load_ratings, summarize

    ratings_path = Path(path)
    all_ratings = load_ratings(ratings_path)

    if not all_ratings:
        click.echo(f"No ratings found in {ratings_path}")
        raise SystemExit(1)

    click.echo(f"Ratings Summary ({len(all_ratings)} sessions)")
    click.echo("=" * 60)
    click.echo()

    if len(all_ratings) < MIN_SAMPLE_SIZE:
        click.echo(f"  Warning: Only {len(all_ratings)} ratings collected.")
        click.echo(
            f"  Not enough data for reliable conclusions (need >= {MIN_SAMPLE_SIZE})."
        )
        click.echo()

    all_scores = [r.rating for r in all_ratings]
    overall_mean = sum(all_scores) / len(all_scores)
    click.echo(f"  Overall: mean={overall_mean:.2f}  n={len(all_ratings)}")
    click.echo()

    result = summarize(all_ratings)
    for dim_name, stats_list in result.items():
        click.echo(f"  By {dim_name}:")
        for s in stats_list:
            stdev_str = f"  stdev={s.stdev:.2f}" if s.stdev is not None else ""
            click.echo(
                f"    {s.value:30s}  "
                f"mean={s.mean:.2f}  "
                f"median={s.median:.2f}  "
                f"n={s.count}{stdev_str}"
            )
        click.echo()


@ratings.command("export")
@click.option("--path", default="ratings.jsonl", help="Path to ratings JSONL file.")
@click.option("--output", required=True, help="Output CSV file path.")
def export_csv(path: str, output: str) -> None:
    """Export ratings as CSV."""
    from codeprobe.ratings.collector import load_ratings

    ratings_path = Path(path)
    all_ratings = load_ratings(ratings_path)

    if not all_ratings:
        click.echo(f"No ratings found in {ratings_path}")
        raise SystemExit(1)

    fieldnames = [
        "ts",
        "rating",
        "model",
        "mcps",
        "skills",
        "task_type",
        "duration_s",
        "tool_calls",
    ]

    csv_path = Path(output)
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in all_ratings:
            writer.writerow(
                {
                    "ts": r.ts,
                    "rating": r.rating,
                    "model": r.config.model or "",
                    "mcps": ",".join(r.config.mcps),
                    "skills": ",".join(r.config.skills),
                    "task_type": r.task_type,
                    "duration_s": r.duration_s if r.duration_s is not None else "",
                    "tool_calls": r.tool_calls if r.tool_calls is not None else "",
                }
            )

    click.echo(f"Exported {len(all_ratings)} ratings to {csv_path}")
