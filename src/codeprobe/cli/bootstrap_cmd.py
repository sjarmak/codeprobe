"""Non-interactive containment-image bootstrap command."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import click

from codeprobe.cli._output_helpers import (
    add_json_flags,
    emit_envelope,
    resolve_mode,
)
from codeprobe.cli.errors import DiagnosticError
from codeprobe.sandbox import runner as container_runner
from codeprobe.sandbox.image_bootstrap import (
    BootstrapResult,
    ImageBootstrapError,
    prepare_images,
)

__all__ = ["bootstrap"]


@click.command("bootstrap")
@add_json_flags
@click.option(
    "--engine",
    type=click.Choice(["docker", "podman"], case_sensitive=False),
    default=None,
    help="Container engine to prepare (default: first available or persisted engine).",
)
@click.option(
    "--agent-image",
    default=None,
    metavar="OCI_REF",
    help="Trusted agent source image; defaults to the configured source reference.",
)
@click.option(
    "--scoring-image",
    default=None,
    metavar="OCI_REF",
    help="Trusted scoring source image; defaults to the configured source reference.",
)
@click.option(
    "--agent-digest",
    default=None,
    metavar="SHA256",
    help="Expected agent digest when --agent-image is tag-based.",
)
@click.option(
    "--scoring-digest",
    default=None,
    metavar="SHA256",
    help="Expected scoring digest when --scoring-image is tag-based.",
)
@click.option(
    "--agent-archive",
    type=click.Path(exists=True, dir_okay=False, readable=True, resolve_path=True, path_type=Path),
    default=None,
    help="Offline OCI archive for the agent image; requires --scoring-archive.",
)
@click.option(
    "--scoring-archive",
    type=click.Path(exists=True, dir_okay=False, readable=True, resolve_path=True, path_type=Path),
    default=None,
    help="Offline OCI archive for the scoring image; requires --agent-archive.",
)
def bootstrap(
    engine: str | None,
    agent_image: str | None,
    scoring_image: str | None,
    agent_digest: str | None,
    scoring_digest: str | None,
    agent_archive: Path | None,
    scoring_archive: Path | None,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Pull or import digest-verified agent and scoring images."""
    mode = resolve_mode("bootstrap", json_flag, no_json_flag, json_lines_flag)
    try:
        agent_source = _source_reference(agent_image, container_runner.agent_source_image_reference)
        scoring_source = _source_reference(scoring_image, container_runner.scoring_source_image_reference)
        result = prepare_images(
            engine=engine,
            agent_reference=agent_source,
            scoring_reference=scoring_source,
            agent_digest=agent_digest,
            scoring_digest=scoring_digest,
            agent_archive=agent_archive,
            scoring_archive=scoring_archive,
            config_path=None,
        )
    except (ImageBootstrapError, ValueError, OSError) as exc:
        raise _bootstrap_failure(str(exc)) from exc
    if mode.mode == "pretty":
        _render_success(result)
    else:
        emit_envelope(command="bootstrap", data=_result_data(result))


def _source_reference(explicit: str | None, resolver: Callable[[], str]) -> str:
    return explicit if explicit is not None else resolver()


def _bootstrap_failure(message: str) -> DiagnosticError:
    return DiagnosticError(
        code="CONTAINER_BOOTSTRAP_FAILED",
        message=message,
        diagnose_cmd="codeprobe bootstrap --help",
        next_steps=[
            ("Inspect configured image inputs", "codeprobe bootstrap --help"),
            ("Check container prerequisites", "codeprobe doctor"),
        ],
    )


def _result_data(result: BootstrapResult) -> dict[str, object]:
    return {
        "engine": result.engine,
        "config_path": str(result.config_path),
        "agent": result.agent.to_dict(),
        "scoring": result.scoring.to_dict(),
    }


def _render_success(result: BootstrapResult) -> None:
    click.echo(f"Containment images are ready with {result.engine}.")
    click.echo(f"  agent:   {result.agent.local_id} ({result.agent.digest})")
    click.echo(f"  scoring: {result.scoring.local_id} ({result.scoring.digest})")
    click.echo(f"  config:  {result.config_path}")
