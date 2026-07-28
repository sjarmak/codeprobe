"""Receiving-side validation for exported evidence bundles."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from codeprobe.snapshot import evidence_directory
from codeprobe.snapshot.evidence_bundle import (
    ARTIFACT_FILENAMES,
    export_evidence_bundle,
    load_evidence_request,
    preview_evidence_bundle,
)
from codeprobe.snapshot.evidence_models import EvidenceBundleValidationError
from codeprobe.snapshot.evidence_review import (
    MAX_EVIDENCE_ARTIFACT_BYTES,
    validate_evidence_bundle_directory,
)
from codeprobe.snapshot.safe_io import SymlinkEscapeError
from tests.snapshot._evidence_helpers import evidence_request


class _UnexpectedEntryStream:
    def __enter__(self) -> _UnexpectedEntryStream:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def __iter__(self) -> Iterator[SimpleNamespace]:
        yield SimpleNamespace(name="unexpected")
        raise AssertionError("scanner consumed entries after the first refusal")


def _export_bundle(
    tmp_path: Path,
    *,
    request_data: dict[str, object] | None = None,
    name: str = "bundle",
) -> tuple[Path, str]:
    request_path = tmp_path / f"{name}-request.json"
    request_path.write_text(
        json.dumps(request_data or evidence_request()),
        encoding="utf-8",
    )
    request = load_evidence_request(request_path)
    preview = preview_evidence_bundle(request)
    bundle_path = tmp_path / name
    export_evidence_bundle(request, bundle_path, preview.approval_digest)
    return bundle_path, preview.approval_digest


def test_received_bundle_returns_only_review_safe_facts(tmp_path: Path) -> None:
    bundle_path, approval_digest = _export_bundle(tmp_path)

    validated = validate_evidence_bundle_directory(
        bundle_path,
        approval_digest,
    )

    assert validated.approval_digest == approval_digest
    assert validated.conclusion == "advance_a"


def test_received_bundle_rejects_malformed_expected_digest(
    tmp_path: Path,
) -> None:
    with pytest.raises(EvidenceBundleValidationError, match="invalid format"):
        validate_evidence_bundle_directory(tmp_path / "missing", "not-a-digest")


def test_received_bundle_rejects_tampered_content(tmp_path: Path) -> None:
    bundle_path, approval_digest = _export_bundle(tmp_path)
    aggregate_path = bundle_path / "aggregate-results.json"
    aggregate = json.loads(aggregate_path.read_text(encoding="utf-8"))
    aggregate["conclusion"] = "advance_b"
    aggregate_path.write_text(json.dumps(aggregate), encoding="utf-8")

    with pytest.raises(EvidenceBundleValidationError, match="findings.md"):
        validate_evidence_bundle_directory(bundle_path, approval_digest)


def test_received_bundle_rejects_non_utf8_artifact(tmp_path: Path) -> None:
    bundle_path, approval_digest = _export_bundle(tmp_path)
    (bundle_path / "findings.md").write_bytes(b"\xff")

    with pytest.raises(EvidenceBundleValidationError, match="UTF-8"):
        validate_evidence_bundle_directory(bundle_path, approval_digest)


def test_directory_scan_stops_at_first_unapproved_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "codeprobe.snapshot.evidence_directory.os.scandir",
        lambda _directory_fd: _UnexpectedEntryStream(),
    )

    with pytest.raises(SymlinkEscapeError, match="exactly"):
        evidence_directory._require_exact_names(
            0,
            frozenset(ARTIFACT_FILENAMES),
        )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO requires POSIX")
def test_received_bundle_rejects_fifo_artifact(tmp_path: Path) -> None:
    bundle_path, approval_digest = _export_bundle(tmp_path)
    findings = bundle_path / "findings.md"
    findings.unlink()
    os.mkfifo(findings)

    with pytest.raises(SymlinkEscapeError, match="not a regular file"):
        validate_evidence_bundle_directory(bundle_path, approval_digest)


def test_received_bundle_bounds_oversized_artifact(tmp_path: Path) -> None:
    bundle_path, approval_digest = _export_bundle(tmp_path)
    (bundle_path / "findings.md").write_bytes(
        b"x" * (MAX_EVIDENCE_ARTIFACT_BYTES + 1)
    )

    with pytest.raises(SymlinkEscapeError, match="size limit"):
        validate_evidence_bundle_directory(bundle_path, approval_digest)


def test_received_bundle_accepts_maximum_task_count(tmp_path: Path) -> None:
    bundle_path, approval_digest = _export_bundle(
        tmp_path,
        request_data=evidence_request(task_count=10_000),
    )

    validated = validate_evidence_bundle_directory(
        bundle_path,
        approval_digest,
    )

    assert validated.approval_digest == approval_digest


def test_received_bundle_requires_out_of_band_approval_digest(
    tmp_path: Path,
) -> None:
    _, expected_digest = _export_bundle(tmp_path, name="expected")
    alternate = evidence_request()
    alternate["run"]["environment"]["network_posture"] = "approved"
    alternate_path, alternate_digest = _export_bundle(
        tmp_path,
        request_data=alternate,
        name="alternate",
    )
    assert alternate_digest != expected_digest

    with pytest.raises(
        EvidenceBundleValidationError,
        match="expected data-owner digest",
    ):
        validate_evidence_bundle_directory(alternate_path, expected_digest)
