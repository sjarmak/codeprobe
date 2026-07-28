"""Unit and integration coverage for the zero-code-access evidence bundle."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from codeprobe.snapshot.evidence_bundle import (
    ARTIFACT_FILENAMES,
    EvidenceApprovalError,
    EvidenceBundleValidationError,
    export_evidence_bundle,
    load_evidence_request,
    preview_evidence_bundle,
    validate_evidence_bundle_documents,
)
from codeprobe.snapshot.evidence_findings import render_findings
from codeprobe.snapshot.safe_io import SymlinkEscapeError
from tests.snapshot._evidence_helpers import evidence_request


def _write_request(tmp_path: Path, request: dict[str, Any]) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "bundle-request.json"
    path.write_text(json.dumps(request))
    return path


def _json_artifact(preview: Any, filename: str) -> dict[str, Any]:
    artifact = next(item for item in preview.artifacts if item.filename == filename)
    return json.loads(artifact.content)


def _documents(preview: Any) -> dict[str, str]:
    return {item.filename: item.content for item in preview.artifacts}


def test_preview_has_exact_five_versioned_artifacts(tmp_path: Path) -> None:
    request = load_evidence_request(_write_request(tmp_path, evidence_request()))

    preview = preview_evidence_bundle(request)

    assert tuple(item.filename for item in preview.artifacts) == ARTIFACT_FILENAMES
    assert preview.approval_digest.startswith("sha256:")
    assert len(preview.approval_digest) == 71
    assert _json_artifact(preview, "run-manifest.json")["schema_version"] == (
        "codeprobe.zero-code-access.run-manifest.v1"
    )
    assert (
        _json_artifact(preview, "sample-attestation.json")["schema_version"]
        == "codeprobe.zero-code-access.sample-attestation.v1"
    )
    assert (
        _json_artifact(preview, "aggregate-results.json")["schema_version"]
        == "codeprobe.zero-code-access.aggregate-results.v1"
    )
    assert _json_artifact(preview, "support-log.json")["schema_version"] == (
        "codeprobe.zero-code-access.support-log.v1"
    )
    attestation = _json_artifact(preview, "sample-attestation.json")[
        "data_owner_attestation"
    ]
    assert attestation == {
        "approval_digest": preview.approval_digest,
        "approval_method": "data_owner_supplied_bound_digest",
        "statements": [
            "privacy",
            "sample_fidelity",
            "result_fidelity",
            "usefulness",
        ],
    }
    findings = next(item.content for item in preview.artifacts if item.filename == "findings.md")
    assert findings.startswith("---\nschema_version: codeprobe.zero-code-access.findings.v1\n")
    assert "conclusion: advance_a" in findings


def test_preview_contains_only_hashes_and_aggregate_results(tmp_path: Path) -> None:
    request = load_evidence_request(_write_request(tmp_path, evidence_request()))

    preview = preview_evidence_bundle(request)
    sample = _json_artifact(preview, "sample-attestation.json")
    aggregate = _json_artifact(preview, "aggregate-results.json")
    all_content = b"\n".join(item.content.encode() for item in preview.artifacts)

    assert len(sample["task_pairs"]) == 10
    assert set(sample["task_pairs"][0]) == {
        "category_id",
        "task_digest",
        "verifier_digest",
    }
    assert aggregate["paired_task_count"] == 10
    assert aggregate["repeats_per_task"] == 3
    for prohibited in (
        b"repository_path",
        b"source",
        b"prompt",
        b"patch",
        b"trace",
        b"task_result",
        b"raw_diagnostic",
    ):
        assert prohibited not in all_content


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ((), "repository_path"),
        (("run",), "repository_name"),
        (("sample", "task_pairs", 0), "prompt"),
        (("results", "configurations", 0), "raw_results"),
        (("finding",), "summary"),
        (("support", "events", 0), "diagnostic"),
    ],
)
def test_request_rejects_every_extra_or_prohibited_field_without_echoing_value(
    tmp_path: Path,
    path: tuple[str | int, ...],
    field: str,
) -> None:
    request = copy.deepcopy(evidence_request())
    target: Any = request
    for part in path:
        target = target[part]
    target[field] = "PRIVATE_DATA_SENTINEL"

    with pytest.raises(EvidenceBundleValidationError) as exc_info:
        load_evidence_request(_write_request(tmp_path, request))

    assert "unexpected field" in str(exc_info.value)
    assert "PRIVATE_DATA_SENTINEL" not in str(exc_info.value)


def test_request_rejects_untrusted_field_name_without_echoing_it(
    tmp_path: Path,
) -> None:
    request = evidence_request()
    request["PRIVATE_DATA_SENTINEL"] = True

    with pytest.raises(EvidenceBundleValidationError) as exc_info:
        load_evidence_request(_write_request(tmp_path, request))

    assert "unexpected field" in str(exc_info.value)
    assert "PRIVATE_DATA_SENTINEL" not in str(exc_info.value)


def test_request_rejects_duplicate_json_field(tmp_path: Path) -> None:
    path = _write_request(tmp_path, evidence_request())
    body = path.read_text()
    path.write_text(
        body.replace(
            '"schema_version":',
            '"schema_version": "codeprobe.zero-code-access.request.v1", '
            '"schema_version":',
            1,
        )
    )

    with pytest.raises(EvidenceBundleValidationError, match="duplicate"):
        load_evidence_request(path)


def test_request_rejects_identifying_category_text(tmp_path: Path) -> None:
    request = evidence_request()
    request["sample"]["task_pairs"][0]["category_id"] = "/private/acme/repo"

    with pytest.raises(EvidenceBundleValidationError, match="category_id"):
        load_evidence_request(_write_request(tmp_path, request))


def test_request_rejects_untrusted_codeprobe_version(tmp_path: Path) -> None:
    request = evidence_request()
    request["run"]["codeprobe_version"] = "PrivateDatasetSentinel"

    with pytest.raises(
        EvidenceBundleValidationError, match="codeprobe_version"
    ) as exc_info:
        load_evidence_request(_write_request(tmp_path, request))

    assert "PrivateDatasetSentinel" not in str(exc_info.value)


def test_request_normalizes_oversized_json_integer_error(tmp_path: Path) -> None:
    path = tmp_path / "oversized-integer.json"
    path.write_text('{"value": ' + ("9" * 5_000) + "}")

    with pytest.raises(EvidenceBundleValidationError, match="valid UTF-8 JSON"):
        load_evidence_request(path)


@pytest.mark.parametrize(
    "invalid_cost",
    [-0.01, float("nan"), int("9" * 400)],
)
def test_request_rejects_invalid_total_cost(
    tmp_path: Path, invalid_cost: float | int
) -> None:
    request = evidence_request()
    request["results"]["configurations"][0]["total_cost_usd"] = invalid_cost

    with pytest.raises(EvidenceBundleValidationError, match="total_cost_usd"):
        load_evidence_request(_write_request(tmp_path, request))


def test_request_rejects_per_category_paired_count_above_selected(
    tmp_path: Path,
) -> None:
    request = evidence_request()
    for pair in request["sample"]["task_pairs"][5:]:
        pair["category_id"] = "category_02"
    request["sample"]["category_counts"] = [
        {
            "category_id": "category_01",
            "selected_count": 5,
            "paired_scorable_count": 6,
        },
        {
            "category_id": "category_02",
            "selected_count": 5,
            "paired_scorable_count": 4,
        },
    ]

    with pytest.raises(
        EvidenceBundleValidationError,
        match=r"category_counts\[0\]",
    ):
        load_evidence_request(_write_request(tmp_path, request))


def test_request_rejects_false_per_category_task_distribution(
    tmp_path: Path,
) -> None:
    request = evidence_request()
    request["sample"]["task_pairs"][-1]["category_id"] = "category_02"
    request["sample"]["category_counts"] = [
        {
            "category_id": "category_01",
            "selected_count": 1,
            "paired_scorable_count": 1,
        },
        {
            "category_id": "category_02",
            "selected_count": 9,
            "paired_scorable_count": 9,
        },
    ]

    with pytest.raises(
        EvidenceBundleValidationError,
        match="category_counts",
    ):
        load_evidence_request(_write_request(tmp_path, request))


def test_other_provider_personnel_direct_access_is_disqualifying(
    tmp_path: Path,
) -> None:
    request = evidence_request(
        conclusion="insufficient_evidence",
        support_events=[
            {
                "sequence": 1,
                "actor_role": "other_provider_personnel",
                "kind": "direct_environment_access",
            }
        ],
    )

    preview = preview_evidence_bundle(
        load_evidence_request(_write_request(tmp_path, request))
    )

    support = _json_artifact(preview, "support-log.json")
    assert support["disqualified"] is True


@pytest.mark.parametrize(
    "request_doc",
    [
        evidence_request(task_count=9),
        evidence_request(repeats=2),
        evidence_request(same_task_set=False),
        evidence_request(changed_after_results=True),
        evidence_request(
            support_events=[
                {
                    "sequence": 1,
                    "actor_role": "provider_support",
                    "kind": "direct_environment_access",
                }
            ]
        ),
    ],
)
def test_advance_conclusion_fails_closed_when_evidence_gate_fails(tmp_path: Path, request_doc: dict[str, Any]) -> None:
    parsed = load_evidence_request(_write_request(tmp_path, request_doc))

    with pytest.raises(
        EvidenceBundleValidationError,
        match="advance conclusion requires sufficient evidence",
    ):
        preview_evidence_bundle(parsed)


def test_insufficient_evidence_records_all_structural_warnings(
    tmp_path: Path,
) -> None:
    request = evidence_request(
        conclusion="insufficient_evidence",
        task_count=9,
        repeats=2,
        same_task_set=False,
        changed_after_results=True,
        support_events=[
            {
                "sequence": 1,
                "actor_role": "provider_engineering",
                "kind": "generic_guidance",
            }
        ],
    )
    preview = preview_evidence_bundle(load_evidence_request(_write_request(tmp_path, request)))

    aggregate = _json_artifact(preview, "aggregate-results.json")
    support = _json_artifact(preview, "support-log.json")
    assert aggregate["evidence_sufficient"] is False
    assert aggregate["validity_warnings"] == [
        "below_paired_task_floor",
        "different_task_sets",
        "disqualifying_support",
        "incomplete_repeats",
        "sample_changed_after_results",
    ]
    assert support["disqualified"] is True


def test_approval_digest_binds_all_previewed_content(tmp_path: Path) -> None:
    first = evidence_request()
    second = evidence_request()
    second["results"]["configurations"][0]["mean_quality"] = 0.73

    first_preview = preview_evidence_bundle(load_evidence_request(_write_request(tmp_path / "first", first)))
    second_preview = preview_evidence_bundle(load_evidence_request(_write_request(tmp_path / "second", second)))

    assert first_preview.approval_digest != second_preview.approval_digest


def test_export_normalizes_malformed_approval_digest(tmp_path: Path) -> None:
    request = load_evidence_request(_write_request(tmp_path, evidence_request()))
    out = tmp_path / "approved-bundle"

    with pytest.raises(EvidenceApprovalError, match="approval digest"):
        export_evidence_bundle(request, out, "é")

    assert not out.exists()


@pytest.mark.integration
def test_export_requires_bound_approval_and_publishes_exactly_once(
    tmp_path: Path,
) -> None:
    request = load_evidence_request(_write_request(tmp_path, evidence_request()))
    preview = preview_evidence_bundle(request)
    out = tmp_path / "approved-bundle"

    with pytest.raises(EvidenceApprovalError):
        export_evidence_bundle(request, out, "sha256:" + ("0" * 64))
    assert not out.exists()

    written = export_evidence_bundle(request, out, preview.approval_digest)
    assert written == out
    assert tuple(sorted(path.name for path in out.iterdir())) == tuple(sorted(ARTIFACT_FILENAMES))

    with pytest.raises(SymlinkEscapeError):
        export_evidence_bundle(request, out, preview.approval_digest)


def test_document_validator_rejects_post_preview_field_injection(
    tmp_path: Path,
) -> None:
    request = load_evidence_request(_write_request(tmp_path, evidence_request()))
    preview = preview_evidence_bundle(request)
    documents = _documents(preview)
    manifest = json.loads(documents["run-manifest.json"])
    manifest["source"] = "PRIVATE_DATA_SENTINEL"
    documents["run-manifest.json"] = json.dumps(manifest)

    with pytest.raises(EvidenceBundleValidationError) as exc_info:
        validate_evidence_bundle_documents(documents)

    assert "unexpected field" in str(exc_info.value)
    assert "PRIVATE_DATA_SENTINEL" not in str(exc_info.value)


def test_document_validator_rejects_duplicate_json_field(
    tmp_path: Path,
) -> None:
    request = load_evidence_request(_write_request(tmp_path, evidence_request()))
    documents = _documents(preview_evidence_bundle(request))
    documents["run-manifest.json"] = documents["run-manifest.json"].replace(
        '"schema_version":',
        '"schema_version": "codeprobe.zero-code-access.run-manifest.v1", '
        '"schema_version":',
        1,
    )

    with pytest.raises(EvidenceBundleValidationError, match="duplicate"):
        validate_evidence_bundle_documents(documents)


def test_document_validator_normalizes_oversized_json_integer(
    tmp_path: Path,
) -> None:
    request = load_evidence_request(_write_request(tmp_path, evidence_request()))
    documents = _documents(preview_evidence_bundle(request))
    documents["run-manifest.json"] = '{"value": ' + ("9" * 5_000) + "}"

    with pytest.raises(EvidenceBundleValidationError, match="valid JSON"):
        validate_evidence_bundle_documents(documents)


def test_document_validator_rejects_false_per_category_task_distribution(
    tmp_path: Path,
) -> None:
    request_doc = evidence_request()
    request_doc["sample"]["task_pairs"][-1]["category_id"] = "category_02"
    request_doc["sample"]["category_counts"] = [
        {
            "category_id": "category_01",
            "selected_count": 9,
            "paired_scorable_count": 9,
        },
        {
            "category_id": "category_02",
            "selected_count": 1,
            "paired_scorable_count": 1,
        },
    ]
    request = load_evidence_request(_write_request(tmp_path, request_doc))
    documents = _documents(preview_evidence_bundle(request))
    sample = json.loads(documents["sample-attestation.json"])
    sample["category_counts"][0]["selected_count"] = 1
    sample["category_counts"][0]["paired_scorable_count"] = 1
    sample["category_counts"][1]["selected_count"] = 9
    sample["category_counts"][1]["paired_scorable_count"] = 9
    documents["sample-attestation.json"] = json.dumps(sample)

    with pytest.raises(
        EvidenceBundleValidationError,
        match="category_counts",
    ):
        validate_evidence_bundle_documents(documents)


def test_document_validator_rejects_structurally_valid_content_change(
    tmp_path: Path,
) -> None:
    request = load_evidence_request(_write_request(tmp_path, evidence_request()))
    documents = _documents(preview_evidence_bundle(request))
    aggregate = json.loads(documents["aggregate-results.json"])
    aggregate["configurations"][0]["mean_quality"] = 0.73
    documents["aggregate-results.json"] = json.dumps(aggregate)
    documents["findings.md"] = render_findings(aggregate)

    with pytest.raises(EvidenceBundleValidationError, match="approval_digest"):
        validate_evidence_bundle_documents(documents)


@pytest.mark.parametrize(
    ("filename", "mutate", "expected_path"),
    [
        (
            "run-manifest.json",
            lambda doc: doc["configurations"][0].__setitem__("configuration_digest", "/private/acme/repo"),
            "configuration_digest",
        ),
        (
            "sample-attestation.json",
            lambda doc: doc["task_pairs"][0].__setitem__("task_digest", "not-a-digest"),
            "task_digest",
        ),
        (
            "sample-attestation.json",
            lambda doc: doc["data_owner_attestation"].__setitem__(
                "approval_digest",
                "sha256:" + ("0" * 64),
            ),
            "approval_digest",
        ),
        (
            "aggregate-results.json",
            lambda doc: doc.__setitem__("validity_warnings", ["PRIVATE_DATA_SENTINEL"]),
            "validity_warnings",
        ),
        (
            "aggregate-results.json",
            lambda doc: doc["configurations"][0].__setitem__(
                "total_cost_usd", int("9" * 400)
            ),
            "total_cost_usd",
        ),
        (
            "support-log.json",
            lambda doc: doc["events"][0].__setitem__("actor_role", "private-repo-owner-name"),
            "actor_role",
        ),
    ],
)
def test_document_validator_rejects_allowed_field_value_substitution(
    tmp_path: Path,
    filename: str,
    mutate: Any,
    expected_path: str,
) -> None:
    request = load_evidence_request(_write_request(tmp_path, evidence_request()))
    documents = _documents(preview_evidence_bundle(request))
    document = json.loads(documents[filename])
    mutate(document)
    documents[filename] = json.dumps(document)

    with pytest.raises(EvidenceBundleValidationError, match=expected_path) as exc_info:
        validate_evidence_bundle_documents(documents)

    assert "PRIVATE_DATA_SENTINEL" not in str(exc_info.value)


def test_request_loader_rejects_symlink(tmp_path: Path) -> None:
    real = _write_request(tmp_path, evidence_request())
    linked = tmp_path / "linked-request.json"
    linked.symlink_to(real)

    with pytest.raises(EvidenceBundleValidationError, match="non-symlink"):
        load_evidence_request(linked)


def test_request_loader_rejects_symlinked_parent(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    real = _write_request(real_directory, evidence_request())
    linked_directory = tmp_path / "linked"
    linked_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(EvidenceBundleValidationError, match="non-symlink"):
        load_evidence_request(linked_directory / real.name)
