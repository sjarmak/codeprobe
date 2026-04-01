"""codeprobe probe — generate micro-benchmark probe tasks from a repository."""

from __future__ import annotations

import json
from pathlib import Path

import click

from codeprobe.probe.generator import DEFAULT_COUNT, MAX_PROBES, MIN_PROBES


@click.command()
@click.argument("repo", type=click.Path(exists=True))
@click.option(
    "--count",
    "-n",
    type=int,
    default=DEFAULT_COUNT,
    help=f"Number of probes to generate ({MIN_PROBES}-{MAX_PROBES}).",
)
@click.option(
    "--lang",
    "-l",
    type=click.Choice(["python", "typescript"]),
    default=None,
    help="Filter by language (default: all supported).",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(),
    default=None,
    help="Output directory (default: <repo>/probes/).",
)
@click.option(
    "--seed",
    "-s",
    type=int,
    default=None,
    help="Random seed for reproducibility.",
)
@click.option(
    "--repo-name",
    type=str,
    default=None,
    help="Repository name for metadata (default: derived from path).",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output JSON summary to stdout.",
)
def probe(
    repo: str,
    count: int,
    lang: str | None,
    output: str | None,
    seed: int | None,
    repo_name: str | None,
    output_json: bool,
) -> None:
    """Generate micro-benchmark probe tasks from a repository.

    Extracts symbols (functions, classes, methods) from Python and TypeScript
    files, generates probe questions with ground-truth answers, and writes
    task directories in the standard eval format.
    """
    from codeprobe.probe.generator import generate_probes
    from codeprobe.probe.writer import write_probe_tasks

    repo_root = Path(repo).resolve()
    count = max(MIN_PROBES, min(MAX_PROBES, count))
    output_dir = Path(output) if output else repo_root / "probes"
    effective_repo_name = repo_name or repo_root.name

    click.echo(f"Scanning {repo_root} for symbols...", err=True)
    probes = generate_probes(
        repo_root=repo_root,
        count=count,
        lang_filter=lang,
        seed=seed,
    )

    if not probes:
        click.echo("No probes generated -- no suitable symbols found.", err=True)
        raise SystemExit(1)

    click.echo(
        f"Generated {len(probes)} probes, writing to {output_dir}...",
        err=True,
    )
    created = write_probe_tasks(probes, output_dir, effective_repo_name)

    # Summary
    by_template: dict[str, int] = {}
    for p in probes:
        by_template[p.template_name] = by_template.get(p.template_name, 0) + 1

    if output_json:
        summary = {
            "total": len(probes),
            "by_template": by_template,
            "output_dir": str(output_dir),
            "tasks": [str(d) for d in created],
        }
        click.echo(json.dumps(summary, indent=2))
    else:
        click.echo(f"Probe generation complete:", err=True)
        click.echo(f"  Total probes: {len(probes)}", err=True)
        for tpl_name, tpl_count in sorted(by_template.items()):
            click.echo(f"  {tpl_name}: {tpl_count}", err=True)
        click.echo(f"  Output: {output_dir}", err=True)
        click.echo(f"Created {len(created)} probe tasks in {output_dir}")
