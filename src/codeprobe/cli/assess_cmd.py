"""codeprobe assess — evaluate a codebase's benchmarking potential."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import click

from codeprobe.calibration import (
    CalibrationProfile,
    format_calibration_line,
)
from codeprobe.cli._output_helpers import emit_envelope, resolve_mode

logger = logging.getLogger(__name__)

# Environment variable users can set to point `codeprobe assess` at a
# previously-emitted calibration profile. When set, the assess output
# includes a `calibration_confidence` surface line.
CALIBRATION_PROFILE_ENV = "CODEPROBE_CALIBRATION_PROFILE"


def load_calibration_profile(
    path: Path | None = None,
) -> CalibrationProfile | None:
    """Best-effort load of a calibration profile from ``path`` or env.

    Never raises: calibration is an optional surface, and a missing or
    malformed profile must not block the core ``assess`` output. On any
    failure the function logs a warning and returns ``None``.
    """
    candidate: Path | None
    if path is not None:
        candidate = path
    else:
        env_value = os.environ.get(CALIBRATION_PROFILE_ENV)
        candidate = Path(env_value) if env_value else None

    if candidate is None or not candidate.exists():
        return None

    try:
        raw = json.loads(candidate.read_text(encoding="utf-8"))
        return CalibrationProfile.from_dict(raw)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "Failed to load calibration profile from %s: %s", candidate, exc
        )
        return None


def run_assess(
    path: str,
    *,
    json_flag: bool = False,
    no_json_flag: bool = False,
    json_lines_flag: bool = False,
) -> None:
    """Assess a codebase for AI agent benchmarking potential."""
    from codeprobe.assess import assess_repo

    mode = resolve_mode(
        "assess", json_flag, no_json_flag, json_lines_flag,
    )

    repo_path = Path(path).resolve()
    if not repo_path.is_dir():
        click.echo(f"Error: {repo_path} is not a directory.", err=True)
        raise SystemExit(1)
    if not (repo_path / ".git").exists():
        click.echo(f"Error: {repo_path} does not appear to be a git repository.", err=True)
        raise SystemExit(1)
    score = assess_repo(repo_path)
    profile = load_calibration_profile()

    if mode.mode == "pretty":
        click.echo(f"Codebase Assessment: {repo_path.name}")
        click.echo(f"{'=' * 50}")
        click.echo()

        method_label = score.scoring_method
        if score.model_used:
            method_label += f" ({score.model_used})"
        click.echo(f"Scoring method: {method_label}")
        click.echo(f"Overall Score: {score.overall:.0%}")
        click.echo()
        click.echo("Breakdown:")
        for dim in score.dimensions:
            click.echo(f"  {dim.name:20s} {dim.score:.0%}  {dim.reasoning}")
        click.echo()
        click.echo(f"Recommendation: {score.recommendation}")

        # Calibration confidence surface (R11). Printed unconditionally so
        # downstream consumers always see the field — either a value or an
        # explicit "unavailable" marker.
        click.echo()
        click.echo(format_calibration_line(profile))

        if score.overall >= 0.5:
            click.echo()
            click.echo("Next: codeprobe mine . --count 5")
        return

    # Envelope / NDJSON mode.
    emit_envelope(
        command="assess",
        data={
            "repo": str(repo_path),
            "overall": score.overall,
            "scoring_method": score.scoring_method,
            "model_used": score.model_used,
            "dimensions": [
                {"name": d.name, "score": d.score, "reasoning": d.reasoning}
                for d in score.dimensions
            ],
            "recommendation": score.recommendation,
            "calibration": format_calibration_line(profile),
        },
    )
