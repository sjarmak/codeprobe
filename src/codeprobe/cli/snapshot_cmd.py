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
) -> None:
    """Create a snapshot of EXPERIMENT_DIR.

    By default the snapshot is metadata-only (sha256 + size per file). To
    export file bodies — even with redaction — pass ``--allow-source-in-export``
    alongside ``--redact=contents`` or ``--redact=secrets``.

    Secrets mode additionally requires a pre-publish canary proof: either
    supply ``--canary-proof <path>`` or run interactively so the CLI can
    prompt you to confirm the scanner caught a planted canary.
    """

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

    click.echo(json.dumps(status, indent=2))


@snapshot.command("verify")
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
def verify_cmd(snapshot_dir: Path, signing_key: str | None) -> None:
    """Verify a snapshot's attestation, symlink containment, and file hashes."""

    result = verify_snapshot_extended(snapshot_dir, signing_key=signing_key)
    click.echo(
        json.dumps(
            {
                "ok": result.ok,
                "reason": result.reason,
                "body_sha256_matches": result.base.body_sha256_matches,
                "signature_matches": result.base.signature_matches,
                "symlinks_contained": result.symlinks_contained,
                "file_hashes_match": result.file_hashes_match,
                "offending_paths": result.offending_paths,
            },
            indent=2,
        )
    )
    if not result.ok:
        sys.exit(1)
