"""Fail-closed validation for received zero-code-access evidence bundles."""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from codeprobe.snapshot.evidence_approval import is_approval_digest
from codeprobe.snapshot.evidence_directory import (
    read_exact_evidence_directory,
)
from codeprobe.snapshot.evidence_models import EvidenceBundleValidationError
from codeprobe.snapshot.evidence_validation import (
    ARTIFACT_FILENAMES,
    validate_evidence_bundle_documents,
)

MAX_EVIDENCE_ARTIFACT_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class ValidatedEvidenceBundle:
    """Reviewer-safe facts from one structurally valid exported bundle."""

    approval_digest: str
    conclusion: str


def _decoded_documents(captured: Mapping[str, bytes]) -> dict[str, str]:
    try:
        return {
            name: captured[name].decode("utf-8")
            for name in ARTIFACT_FILENAMES
        }
    except UnicodeDecodeError as error:
        raise EvidenceBundleValidationError(
            "bundle: every artifact must be UTF-8 text"
        ) from error


def validate_evidence_bundle_directory(
    bundle_path: Path,
    expected_approval_digest: str,
) -> ValidatedEvidenceBundle:
    """Securely load and validate one received five-artifact bundle."""
    if not is_approval_digest(expected_approval_digest):
        raise EvidenceBundleValidationError(
            "approval_digest: expected digest has an invalid format"
        )
    captured = read_exact_evidence_directory(
        bundle_path,
        ARTIFACT_FILENAMES,
        max_artifact_bytes=MAX_EVIDENCE_ARTIFACT_BYTES,
    )
    documents = _decoded_documents(captured)
    validate_evidence_bundle_documents(documents)
    manifest = cast(
        dict[str, object],
        json.loads(documents["run-manifest.json"]),
    )
    aggregate = cast(
        dict[str, object],
        json.loads(documents["aggregate-results.json"]),
    )
    approval_digest = cast(str, manifest["approval_digest"])
    if not hmac.compare_digest(approval_digest, expected_approval_digest):
        raise EvidenceBundleValidationError(
            "approval_digest: does not match expected data-owner digest"
        )
    return ValidatedEvidenceBundle(
        approval_digest=approval_digest,
        conclusion=cast(str, aggregate["conclusion"]),
    )


__all__ = [
    "MAX_EVIDENCE_ARTIFACT_BYTES",
    "ValidatedEvidenceBundle",
    "validate_evidence_bundle_directory",
]
