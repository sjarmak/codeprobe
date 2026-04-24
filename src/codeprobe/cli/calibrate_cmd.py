"""codeprobe calibrate — run the R11 inter-curator agreement gate and emit
a :class:`~codeprobe.calibration.profile.CalibrationProfile` when it passes.

Usage::

    codeprobe calibrate HOLDOUT_PATH \\
        --curator-version v1 \\
        [--threshold 0.6] \\
        [--min-tasks 100] \\
        [--min-repos 3] \\
        [--out path/to/profile.json]

Exit codes:

* ``0`` — profile emitted (gate passed).
* ``1`` — gate rejected the profile (reason printed to stderr).

The JSON output always includes a ``calibration_confidence`` field so that
downstream surfaces (``codeprobe assess``) can display it without needing
to know internal field names.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from codeprobe.calibration import (
    CalibrationRejectedError,
    emit_profile,
    load_holdout,
)
from codeprobe.cli._output_helpers import (
    add_json_flags,
    emit_envelope,
    resolve_mode,
)
from codeprobe.cli.errors import DiagnosticError
from codeprobe.config.defaults import resolve_out_calibrate, use_v07_defaults


@click.command("calibrate")
@add_json_flags
@click.argument("holdout_path", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--curator-version",
    required=True,
    help="Version identifier for the curator model/harness used to score the holdout.",
)
@click.option(
    "--threshold",
    default=0.6,
    type=click.FloatRange(-1.0, 1.0),
    show_default=True,
    help="Minimum Pearson correlation required to emit a profile.",
)
@click.option(
    "--min-tasks",
    default=100,
    type=click.IntRange(min=1),
    show_default=True,
    help="Minimum number of holdout tasks (R11 requires >=100).",
)
@click.option(
    "--min-repos",
    default=3,
    type=click.IntRange(min=1),
    show_default=True,
    help="Minimum distinct repos represented in the holdout (R11 requires >=3).",
)
@click.option(
    "--out",
    type=click.Path(dir_okay=False, writable=True),
    default=None,
    help="Optional path to write the emitted profile JSON.",
)
def calibrate(
    holdout_path: str,
    curator_version: str,
    threshold: float,
    min_tasks: int,
    min_repos: int,
    out: str | None,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Run the calibration gate and emit a profile when it passes.

    A profile is emitted ONLY when:

    1. The holdout has >= --min-tasks rows.
    2. The holdout spans >= --min-repos distinct repositories.
    3. Pearson correlation between the two curators is >= --threshold.

    Any failure prints the reason to stderr and exits 1 without writing a
    profile. This is the R11 validity gate.
    """
    mode = resolve_mode(
        "calibrate", json_flag, no_json_flag, json_lines_flag,
    )
    holdout_file = Path(holdout_path)

    try:
        rows = load_holdout(holdout_file)
        profile = emit_profile(
            rows,
            curator_version=curator_version,
            threshold=threshold,
            min_tasks=min_tasks,
            min_repos=min_repos,
        )
    except CalibrationRejectedError as exc:
        raise DiagnosticError(
            code="CALIBRATION_REJECTED",
            message=f"calibration_rejected: {exc}",
            diagnose_cmd="codeprobe interpret <holdout> --json",
            terminal=True,
        ) from exc

    payload = profile.to_dict()
    output = json.dumps(payload, indent=2, sort_keys=True)

    # Under v0.7 defaults, auto-resolve an output path when the user
    # did not pass --out so the profile is persisted to a predictable
    # location. Pre-v0.7 behavior (stdout-only when --out is omitted)
    # is preserved.
    if out is None and use_v07_defaults():
        resolved, _ = resolve_out_calibrate(curator_version)
        out = str(resolved)

    if out is not None:
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output + "\n", encoding="utf-8")

    if mode.mode == "pretty":
        click.echo(output)
        if out is not None:
            click.echo(f"Wrote profile to {Path(out)}", err=True)
        return

    emit_envelope(
        command="calibrate",
        data={
            "profile": payload,
            "out_path": str(Path(out)) if out is not None else None,
        },
    )
