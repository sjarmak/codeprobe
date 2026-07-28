"""Build and atomically export the zero-code-access evidence bundle."""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from codeprobe.snapshot.evidence_approval import (
    APPROVAL_PLACEHOLDER,
    approval_digest_for_documents,
    is_approval_digest,
)
from codeprobe.snapshot.evidence_findings import render_findings
from codeprobe.snapshot.evidence_models import (
    MIN_PAIRED_TASKS,
    MIN_REPEATS_PER_TASK,
    EvidenceBundleValidationError,
    EvidenceRequest,
)
from codeprobe.snapshot.evidence_schema import load_evidence_request
from codeprobe.snapshot.evidence_validation import (
    AGGREGATE_RESULTS_SCHEMA,
    APPROVAL_STATEMENTS,
    ARTIFACT_FILENAMES,
    RUN_MANIFEST_SCHEMA,
    SAMPLE_ATTESTATION_SCHEMA,
    SUPPORT_LOG_SCHEMA,
    validate_evidence_bundle_documents,
)
from codeprobe.snapshot.safe_io import staged_output_directory


class EvidenceApprovalError(ValueError):
    """Raised when export approval is absent or bound to different content."""


@dataclass(frozen=True)
class EvidenceArtifact:
    """One immutable proposed or exported artifact."""

    filename: str
    content: str


@dataclass(frozen=True)
class EvidenceBundlePreview:
    """The exact five artifacts bound by one approval digest."""

    approval_digest: str
    artifacts: tuple[EvidenceArtifact, ...]


def _validity_warnings(request: EvidenceRequest) -> tuple[str, ...]:
    declared = frozenset(request.results.validity_warnings)
    derived = frozenset(
        warning
        for condition, warning in (
            (
                request.results.paired_task_count < MIN_PAIRED_TASKS,
                "below_paired_task_floor",
            ),
            (
                request.results.repeats_per_task < MIN_REPEATS_PER_TASK,
                "incomplete_repeats",
            ),
            (not request.results.paired_task_set_same, "different_task_sets"),
            (
                request.sample.changed_after_results,
                "sample_changed_after_results",
            ),
            (not request.sample.representative, "sample_not_representative"),
            (
                not request.results.comparison.report_comparable,
                "report_refused",
            ),
            (request.support.disqualified, "disqualifying_support"),
        )
        if condition
    )
    return tuple(sorted(declared | derived))


def _run_manifest(request: EvidenceRequest, approval_digest: str) -> dict[str, Any]:
    return {
        "schema_version": RUN_MANIFEST_SCHEMA,
        "approval_digest": approval_digest,
        "artifact_names": ARTIFACT_FILENAMES,
        "codeprobe_version": request.run.codeprobe_version,
        "environment": asdict(request.run.environment),
        "configurations": tuple(asdict(item) for item in request.run.configurations),
        "run_counts": tuple(
            {
                "configuration_id": item.configuration_id,
                "scorable_run_count": item.scorable_run_count,
                "total_run_count": item.total_run_count,
            }
            for item in request.results.configurations
        ),
    }


def _sample_attestation(request: EvidenceRequest, approval_digest: str) -> dict[str, Any]:
    return {
        "schema_version": SAMPLE_ATTESTATION_SCHEMA,
        "approval_digest": approval_digest,
        "window": asdict(request.sample.window),
        "selection_method": request.sample.selection_method,
        "changed_after_results": request.sample.changed_after_results,
        "task_pairs": tuple(asdict(item) for item in request.sample.task_pairs),
        "category_counts": tuple(asdict(item) for item in request.sample.category_counts),
        "exclusions": tuple(asdict(item) for item in request.sample.exclusions),
        "attrition_count": request.sample.attrition_count,
        "representative": request.sample.representative,
        "data_owner_attestation": {
            "approval_method": "data_owner_supplied_bound_digest",
            "approval_digest": approval_digest,
            "statements": APPROVAL_STATEMENTS,
        },
    }


def _aggregate_results(
    request: EvidenceRequest,
    approval_digest: str,
    warnings: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema_version": AGGREGATE_RESULTS_SCHEMA,
        "approval_digest": approval_digest,
        "conclusion": request.finding.conclusion,
        "evidence_sufficient": not warnings,
        "quality_metric": request.results.quality_metric,
        "repeats_per_task": request.results.repeats_per_task,
        "paired_task_count": request.results.paired_task_count,
        "paired_task_set_same": request.results.paired_task_set_same,
        "configurations": tuple(asdict(item) for item in request.results.configurations),
        "comparison": asdict(request.results.comparison),
        "validity_warnings": warnings,
    }


def _support_log(request: EvidenceRequest, approval_digest: str) -> dict[str, Any]:
    return {
        "schema_version": SUPPORT_LOG_SCHEMA,
        "approval_digest": approval_digest,
        "disqualified": request.support.disqualified,
        "events": tuple(asdict(item) for item in request.support.events),
    }


def _json_content(document: Mapping[str, Any]) -> str:
    return (
        json.dumps(
            document,
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _build_documents(request: EvidenceRequest, approval_digest: str) -> dict[str, str]:
    warnings = _validity_warnings(request)
    if request.finding.conclusion != "insufficient_evidence" and warnings:
        raise EvidenceBundleValidationError("finding.conclusion: advance conclusion requires sufficient evidence")
    run = _run_manifest(request, approval_digest)
    sample = _sample_attestation(request, approval_digest)
    aggregate = _aggregate_results(request, approval_digest, warnings)
    support = _support_log(request, approval_digest)
    return {
        "run-manifest.json": _json_content(run),
        "sample-attestation.json": _json_content(sample),
        "aggregate-results.json": _json_content(aggregate),
        "findings.md": render_findings(aggregate),
        "support-log.json": _json_content(support),
    }


def preview_evidence_bundle(
    request: EvidenceRequest,
) -> EvidenceBundlePreview:
    """Return the exact export content and digest without writing files."""
    unbound_documents = _build_documents(request, APPROVAL_PLACEHOLDER)
    digest = approval_digest_for_documents(unbound_documents)
    documents = _build_documents(request, digest)
    validate_evidence_bundle_documents(documents)
    return EvidenceBundlePreview(
        approval_digest=digest,
        artifacts=tuple(EvidenceArtifact(filename=name, content=documents[name]) for name in ARTIFACT_FILENAMES),
    )


def export_evidence_bundle(
    request: EvidenceRequest,
    out_path: Path,
    approved_digest: str,
) -> Path:
    """Atomically publish the exact preview only after bound approval."""
    if not is_approval_digest(approved_digest):
        raise EvidenceApprovalError(
            "approval digest does not match the current evidence preview"
        )
    preview = preview_evidence_bundle(request)
    if not hmac.compare_digest(preview.approval_digest, approved_digest):
        raise EvidenceApprovalError("approval digest does not match the current evidence preview")
    with staged_output_directory(Path(out_path)) as output:
        for artifact in preview.artifacts:
            output.write_bytes(artifact.filename, artifact.content.encode())
    return Path(out_path)


__all__ = [
    "ARTIFACT_FILENAMES",
    "EvidenceApprovalError",
    "EvidenceArtifact",
    "EvidenceBundlePreview",
    "EvidenceBundleValidationError",
    "export_evidence_bundle",
    "load_evidence_request",
    "preview_evidence_bundle",
    "validate_evidence_bundle_documents",
]
