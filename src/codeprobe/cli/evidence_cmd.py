"""Data-owner-approved zero-code-access evidence bundle commands."""

from __future__ import annotations

import json
from pathlib import Path

import click

from codeprobe.cli._error_handler import CodeprobeGroup
from codeprobe.cli._output_helpers import (
    add_json_flags,
    emit_envelope,
    resolve_mode,
)
from codeprobe.cli.errors import DiagnosticError
from codeprobe.snapshot.evidence_bundle import (
    ARTIFACT_FILENAMES,
    EvidenceApprovalError,
    EvidenceBundlePreview,
    EvidenceBundleValidationError,
    export_evidence_bundle,
    load_evidence_request,
    preview_evidence_bundle,
)
from codeprobe.snapshot.evidence_review import (
    validate_evidence_bundle_directory,
)
from codeprobe.snapshot.evidence_validation import APPROVAL_STATEMENTS
from codeprobe.snapshot.safe_io import SymlinkEscapeError


@click.group("evidence", cls=CodeprobeGroup)
def evidence_cmd() -> None:
    """Preview, export, and validate zero-code-access evidence bundles."""


def _validation_error(
    error: EvidenceBundleValidationError,
) -> DiagnosticError:
    return DiagnosticError(
        code="METADATA_INVALID",
        message=f"Evidence bundle request is invalid: {error}",
        diagnose_cmd="codeprobe snapshot evidence preview REQUEST",
        terminal=True,
    )


def _preview_payload(preview: EvidenceBundlePreview) -> dict[str, object]:
    return {
        "approval_required": True,
        "approval_digest": preview.approval_digest,
        "approval_statements": list(APPROVAL_STATEMENTS),
        "artifacts": list(ARTIFACT_FILENAMES),
        "documents": {
            artifact.filename: artifact.content
            for artifact in preview.artifacts
        },
    }


@evidence_cmd.command("preview")
@add_json_flags
@click.argument(
    "request_path",
    metavar="REQUEST",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
)
def evidence_preview_cmd(
    request_path: Path,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Preview the exact five artifacts and print their approval digest."""
    mode = resolve_mode(
        "snapshot evidence preview",
        json_flag,
        no_json_flag,
        json_lines_flag,
    )
    try:
        preview = preview_evidence_bundle(load_evidence_request(request_path))
    except EvidenceBundleValidationError as error:
        raise _validation_error(error) from error
    payload = _preview_payload(preview)
    if mode.mode == "pretty":
        click.echo(json.dumps(payload, indent=2))
    else:
        emit_envelope(command="snapshot evidence preview", data=payload)


@evidence_cmd.command("export")
@add_json_flags
@click.argument(
    "request_path",
    metavar="REQUEST",
    type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--out",
    "out_path",
    required=True,
    type=click.Path(path_type=Path),
    help="New output directory for the approved five-artifact bundle.",
)
@click.option(
    "--approve",
    "approved_digest",
    required=True,
    help="Exact approval_digest printed by the reviewed preview.",
)
def evidence_export_cmd(
    request_path: Path,
    out_path: Path,
    approved_digest: str,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Atomically export only the exact data-owner-approved preview."""
    mode = resolve_mode(
        "snapshot evidence export",
        json_flag,
        no_json_flag,
        json_lines_flag,
    )
    try:
        request = load_evidence_request(request_path)
        written = export_evidence_bundle(request, out_path, approved_digest)
    except EvidenceBundleValidationError as error:
        raise _validation_error(error) from error
    except EvidenceApprovalError as error:
        raise DiagnosticError(
            code="EVIDENCE_APPROVAL_MISMATCH",
            message=str(error),
            diagnose_cmd="codeprobe snapshot evidence preview REQUEST",
            terminal=True,
        ) from error
    except (OSError, SymlinkEscapeError) as error:
        raise DiagnosticError(
            code="SNAPSHOT_CREATE_FAILED",
            message=f"Evidence bundle export refused: {error}",
            diagnose_cmd=(
                "codeprobe snapshot evidence export REQUEST "
                "--out NEW_DIR --approve DIGEST"
            ),
            terminal=True,
        ) from error
    payload = {
        "approval_digest": approved_digest,
        "artifacts": list(ARTIFACT_FILENAMES),
        "out": str(written),
    }
    if mode.mode == "pretty":
        click.echo(json.dumps(payload, indent=2))
    else:
        emit_envelope(command="snapshot evidence export", data=payload)


@evidence_cmd.command("validate")
@add_json_flags
@click.argument(
    "bundle_path",
    metavar="BUNDLE",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--expect",
    "expected_approval_digest",
    required=True,
    help="Data-owner preview digest received through a trusted channel.",
)
def evidence_validate_cmd(
    bundle_path: Path,
    expected_approval_digest: str,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Validate a received five-artifact evidence bundle."""
    mode = resolve_mode(
        "snapshot evidence validate",
        json_flag,
        no_json_flag,
        json_lines_flag,
    )
    try:
        bundle = validate_evidence_bundle_directory(
            bundle_path,
            expected_approval_digest,
        )
    except EvidenceBundleValidationError as error:
        raise DiagnosticError(
            code="EVIDENCE_BUNDLE_INVALID",
            message=f"Evidence bundle is invalid: {error}",
            diagnose_cmd=(
                "codeprobe snapshot evidence validate BUNDLE "
                "--expect TRUSTED_DIGEST"
            ),
            terminal=True,
        ) from error
    except (OSError, SymlinkEscapeError) as error:
        raise DiagnosticError(
            code="EVIDENCE_BUNDLE_INVALID",
            message=f"Evidence bundle cannot be read securely: {error}",
            diagnose_cmd=(
                "codeprobe snapshot evidence validate BUNDLE "
                "--expect TRUSTED_DIGEST"
            ),
            terminal=True,
        ) from error
    payload = {
        "approval_digest": bundle.approval_digest,
        "conclusion": bundle.conclusion,
    }
    if mode.mode == "pretty":
        click.echo(json.dumps(payload, indent=2))
    else:
        emit_envelope(command="snapshot evidence validate", data=payload)
