"""``codeprobe snapshot`` — produce shareable snapshots with a safe default.

Subcommands:

- ``snapshot create`` — emit a ``SNAPSHOT.json`` (and optionally a redacted
  body tree) from an experiment directory.
- ``snapshot verify`` — recompute hashes and verify the signed attestation.

The default redaction mode is ``hashes-only`` and is the only mode this CLI
accepts without the explicit ``--allow-source-in-export`` opt-in.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from codeprobe.cli._output_helpers import (
    add_json_flags,
    emit_envelope,
    resolve_mode,
)
from codeprobe.snapshot.canary import (
    CANARY_DEFAULT,
    CanaryFailed,
    CanaryGate,
    load_canary_proof,
)
from codeprobe.snapshot.create import (
    SymlinkEscapeError,
    create_snapshot,
)
from codeprobe.snapshot.exporters import (
    export_browse,
    export_datadog,
    export_sheets,
    export_sigma,
)
from codeprobe.snapshot.redact import (
    PUBLISHABLE_DEFAULT,
    RedactionMode,
)
from codeprobe.snapshot.scanners import (
    GitleaksScanner,
    PatternScanner,
    Scanner,
    ScannerUnavailable,
    TrufflehogScanner,
)
from codeprobe.snapshot.verify import verify_snapshot_extended

_VALID_MODES: tuple[RedactionMode, ...] = ("hashes-only", "contents", "secrets")


@click.group()
def snapshot() -> None:
    """Create and verify shareable snapshots of experiment directories."""


def _build_scanner(name: str) -> Scanner:
    """Resolve a scanner by name.

    ``pattern`` is always available. ``gitleaks`` and ``trufflehog`` require
    the corresponding binary on PATH — scanning with them raises
    :class:`ScannerUnavailable` at runtime if missing.
    """

    if name == "pattern":
        return PatternScanner()
    if name == "gitleaks":
        return GitleaksScanner()
    if name == "trufflehog":
        return TrufflehogScanner()
    raise click.UsageError(
        f"unknown --scanner {name!r} (choose: pattern, gitleaks, trufflehog)"
    )


@snapshot.command("create")
@add_json_flags
@click.argument(
    "experiment_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--out",
    "out_path",
    required=True,
    type=click.Path(path_type=Path),
    help="Output directory for the snapshot (SNAPSHOT.json lives here).",
)
@click.option(
    "--redact",
    "mode",
    type=click.Choice(list(_VALID_MODES), case_sensitive=False),
    default=PUBLISHABLE_DEFAULT,
    show_default=True,
    help=(
        "Redaction mode. 'hashes-only' is the publishable default. "
        "'contents' and 'secrets' copy redacted bodies and require "
        "--allow-source-in-export."
    ),
)
@click.option(
    "--allow-source-in-export",
    is_flag=True,
    default=False,
    help=(
        "Required for --redact=contents or --redact=secrets. Acknowledges "
        "that the snapshot will contain file bodies (after scanner redaction)."
    ),
)
@click.option(
    "--scanner",
    "scanner_name",
    default="pattern",
    show_default=True,
    help="Scanner to use: pattern, gitleaks, trufflehog.",
)
@click.option(
    "--canary-proof",
    "canary_proof_path",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Path to a pre-recorded canary proof JSON. Required for "
        "--redact=secrets in non-interactive contexts."
    ),
)
@click.option(
    "--signing-key",
    default=None,
    envvar="CODEPROBE_SIGNING_KEY",
    help=(
        "HMAC signing key for SNAPSHOT.json attestation. Falls back to "
        "CODEPROBE_SIGNING_KEY env var. When unset, manifest is written as "
        "'unsigned' (body hash only)."
    ),
)
def create_cmd(
    experiment_dir: Path,
    out_path: Path,
    mode: str,
    allow_source_in_export: bool,
    scanner_name: str,
    canary_proof_path: Path | None,
    signing_key: str | None,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Create a snapshot of EXPERIMENT_DIR.

    By default the snapshot is metadata-only (sha256 + size per file). To
    export file bodies — even with redaction — pass ``--allow-source-in-export``
    alongside ``--redact=contents`` or ``--redact=secrets``.

    Secrets mode additionally requires a pre-publish canary proof: either
    supply ``--canary-proof <path>`` or run interactively so the CLI can
    prompt you to confirm the scanner caught a planted canary.
    """
    out_mode = resolve_mode(
        "snapshot create", json_flag, no_json_flag, json_lines_flag,
    )

    mode_cast: RedactionMode = mode  # type: ignore[assignment]

    if mode_cast in ("contents", "secrets") and not allow_source_in_export:
        click.echo(
            f"Refusing --redact={mode_cast} without --allow-source-in-export. "
            "This flag is required to acknowledge that file bodies will be "
            "written (after scanner redaction). See docs/SNAPSHOT_REDACTION.md.",
            err=True,
        )
        sys.exit(2)

    scanner: Scanner | None = None
    if mode_cast in ("contents", "secrets"):
        scanner = _build_scanner(scanner_name)

    canary_result = None
    if mode_cast == "secrets":
        if canary_proof_path is not None:
            canary_result = load_canary_proof(canary_proof_path)
            if not canary_result.passed:
                click.echo(
                    f"Canary proof at {canary_proof_path} is marked passed=False. "
                    "Refusing to create a secrets-mode snapshot.",
                    err=True,
                )
                sys.exit(3)
        else:
            if not sys.stdin.isatty():
                click.echo(
                    "--redact=secrets requires --canary-proof <path> in "
                    "non-interactive contexts. Either provide the proof file "
                    "or run this command in a TTY so you can paste the canary "
                    "string interactively.",
                    err=True,
                )
                sys.exit(4)
            click.echo(
                f"Interactive canary gate. Paste this canary to continue:\n"
                f"  {CANARY_DEFAULT}"
            )
            pasted = click.prompt("canary", default="", show_default=False)
            if pasted.strip() != CANARY_DEFAULT:
                click.echo("Canary mismatch. Aborting.", err=True)
                sys.exit(5)
            assert scanner is not None
            try:
                canary_result = CanaryGate(scanner).require_pass_or_raise()
            except CanaryFailed as e:
                click.echo(str(e), err=True)
                sys.exit(6)

    try:
        status = create_snapshot(
            experiment_dir=experiment_dir,
            out_dir=out_path,
            mode=mode_cast,
            scanner=scanner,
            signing_key=signing_key,
            canary_proof=canary_result,
            allow_source_in_export=allow_source_in_export,
        )
    except (
        PermissionError,
        CanaryFailed,
        ScannerUnavailable,
        FileNotFoundError,
        SymlinkEscapeError,
    ) as e:
        click.echo(f"Snapshot failed: {e}", err=True)
        sys.exit(7)

    if out_mode.mode == "pretty":
        click.echo(json.dumps(status, indent=2))
    else:
        emit_envelope(command="snapshot create", data={"status": status})


@snapshot.command("verify")
@add_json_flags
@click.argument(
    "snapshot_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--signing-key",
    default=None,
    envvar="CODEPROBE_SIGNING_KEY",
    help="HMAC key (falls back to CODEPROBE_SIGNING_KEY). Required for hmac-signed manifests.",
)
def verify_cmd(
    snapshot_dir: Path,
    signing_key: str | None,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Verify a snapshot's attestation, symlink containment, and file hashes."""
    mode = resolve_mode(
        "snapshot verify", json_flag, no_json_flag, json_lines_flag,
    )

    result = verify_snapshot_extended(snapshot_dir, signing_key=signing_key)
    payload = {
        "ok": result.ok,
        "reason": result.reason,
        "body_sha256_matches": result.base.body_sha256_matches,
        "signature_matches": result.base.signature_matches,
        "symlinks_contained": result.symlinks_contained,
        "file_hashes_match": result.file_hashes_match,
        "offending_paths": result.offending_paths,
    }
    if mode.mode == "pretty":
        click.echo(json.dumps(payload, indent=2))
    else:
        emit_envelope(
            command="snapshot verify",
            ok=result.ok,
            data=payload,
        )
    if not result.ok:
        sys.exit(1)


_EXPORT_FORMATS: tuple[str, ...] = ("datadog", "sigma", "sheets", "browse")


@snapshot.command("export")
@add_json_flags
@click.argument(
    "snapshot_dir",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(list(_EXPORT_FORMATS), case_sensitive=False),
    required=True,
    help="Export format: datadog, sigma, sheets, or browse.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(path_type=Path),
    default=None,
    help=(
        "Output path. Defaults: datadog -> <snapshot>/datadog.json, "
        "sigma -> <snapshot>/ (emits sigma_results.csv + sigma_schema.json), "
        "sheets -> <snapshot>/sheets.tsv, browse -> <snapshot>/browse.html."
    ),
)
def export_cmd(
    snapshot_dir: Path,
    fmt: str,
    out_path: Path | None,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Export SNAPSHOT_DIR into an observability artefact.

    The export subcommand is a pure local transform — no network calls are
    issued. Callers ship the generated artefact to the downstream system
    (Datadog intake, Sigma/dbt, Google Sheets, or a browser) themselves.
    """
    mode = resolve_mode(
        "snapshot export", json_flag, no_json_flag, json_lines_flag,
    )

    fmt_normalised = fmt.lower()

    try:
        if fmt_normalised == "datadog":
            target = out_path if out_path is not None else snapshot_dir / "datadog.json"
            written = export_datadog(snapshot_dir, target)
            payload = {"format": "datadog", "out": str(written)}
        elif fmt_normalised == "sigma":
            target_dir = out_path if out_path is not None else snapshot_dir
            csv_path, schema_path = export_sigma(snapshot_dir, target_dir)
            payload = {
                "format": "sigma",
                "csv": str(csv_path),
                "schema": str(schema_path),
            }
        elif fmt_normalised == "sheets":
            target = out_path if out_path is not None else snapshot_dir / "sheets.tsv"
            written = export_sheets(snapshot_dir, target)
            payload = {"format": "sheets", "out": str(written)}
        elif fmt_normalised == "browse":
            target = out_path if out_path is not None else snapshot_dir / "browse.html"
            written = export_browse(snapshot_dir, target)
            payload = {"format": "browse", "out": str(written)}
        else:  # pragma: no cover — Click choice guards this branch.
            raise click.UsageError(f"unknown --format {fmt!r}")
    except FileNotFoundError as e:
        click.echo(f"Export failed: {e}", err=True)
        sys.exit(8)

    if mode.mode == "pretty":
        click.echo(json.dumps(payload, indent=2))
    else:
        emit_envelope(command="snapshot export", data=payload)
