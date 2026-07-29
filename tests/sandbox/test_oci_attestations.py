"""Tests for BuildKit OCI attestation payload verification."""

from __future__ import annotations

import copy
import gzip
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from codeprobe.sandbox.oci_attestations import (
    AttestationVerificationError,
    _buildkit_subject_purl,
    _docker_raw_manifest,
    _load_statement,
    _oras_blob_fetch,
    verify_buildkit_attestations,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent
    / "fixtures"
    / "oci_attestations"
    / "valid.json"
)
AMD64_ATTESTATION_REF = (
    "example.test/platform/codeprobe-agent@sha256:"
    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
)
AMD64_SPDX_REF = (
    "example.test/platform/codeprobe-agent@sha256:"
    "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
)
AMD64_SLSA_REF = (
    "example.test/platform/codeprobe-agent@sha256:"
    "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
)
NEW_DIGEST = "sha256:3333333333333333333333333333333333333333333333333333333333333333"


@pytest.fixture()
def attestation_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _verify(fixture: dict[str, Any]) -> None:
    raw_manifests = fixture["raw_manifests"]
    statements = fixture["statements"]

    def raw_manifest(ref: str) -> dict[str, Any]:
        loaded = raw_manifests[ref]
        assert isinstance(loaded, dict)
        return loaded

    def blob_fetch(ref: str) -> bytes:
        return json.dumps(statements[ref], sort_keys=True).encode("utf-8")

    verify_buildkit_attestations(
        image_ref=fixture["image_ref"],
        candidate_ref=fixture["candidate_ref"],
        digest_ref=fixture["digest_ref"],
        raw_manifest=raw_manifest,
        blob_fetch=blob_fetch,
    )


def _index_manifests(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    manifests = fixture["raw_manifests"][fixture["digest_ref"]]["manifests"]
    assert isinstance(manifests, list)
    return manifests


def _amd64_attestation_layers(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    layers = fixture["raw_manifests"][AMD64_ATTESTATION_REF]["layers"]
    assert isinstance(layers, list)
    return layers


def _statement(fixture: dict[str, Any], ref: str = AMD64_SPDX_REF) -> dict[str, Any]:
    statement = fixture["statements"][ref]
    assert isinstance(statement, dict)
    return statement


def test_verify_buildkit_attestations_accepts_valid_fixture(
    attestation_fixture: dict[str, Any],
) -> None:
    _verify(attestation_fixture)


def test_buildkit_subject_purl_encodes_registry_port() -> None:
    assert _buildkit_subject_purl(
        "registry.example:5000/namespace/image:1.2.3-1-1-abcdefabcdef",
        "linux/amd64",
    ) == (
        "pkg:docker/registry.example%3A5000/namespace/image"
        "@1.2.3-1-1-abcdefabcdef?platform=linux%2Famd64"
    )


def test_buildkit_subject_purl_encodes_ipv6_registry() -> None:
    assert _buildkit_subject_purl(
        "[2001:db8::1]:5000/namespace/image:1.2.3-1-1-abcdefabcdef",
        "linux/arm64",
    ) == (
        "pkg:docker/%5B2001%3Adb8%3A%3A1%5D%3A5000/namespace/image"
        "@1.2.3-1-1-abcdefabcdef?platform=linux%2Farm64"
    )


def test_verify_buildkit_attestations_rejects_fake_in_toto_type(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    statement = fixture["statements"][
        "example.test/platform/codeprobe-agent@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    ]
    statement["_type"] = "https://example.test/not-in-toto"

    with pytest.raises(AttestationVerificationError, match="in-toto"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_statement_v0_1_type(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    statement = fixture["statements"][
        "example.test/platform/codeprobe-agent@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    ]
    statement["_type"] = "https://in-toto.io/Statement/v0.1"

    with pytest.raises(AttestationVerificationError, match="in-toto"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_annotation_payload_predicate_mismatch(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    statement = fixture["statements"][
        "example.test/platform/codeprobe-agent@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    ]
    statement["predicateType"] = "https://slsa.dev/provenance/v1"

    with pytest.raises(AttestationVerificationError, match="predicate mismatch"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_slsa_prefix_version(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    layer = _amd64_attestation_layers(fixture)[1]
    layer["annotations"]["in-toto.io/predicate-type"] = (
        "https://slsa.dev/provenance/v1.1"
    )
    _statement(fixture, AMD64_SLSA_REF)["predicateType"] = (
        "https://slsa.dev/provenance/v1.1"
    )

    with pytest.raises(AttestationVerificationError, match="unsupported predicate"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_subject_name_mismatch(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    statement = fixture["statements"][
        "example.test/platform/codeprobe-agent@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    ]
    statement["subject"][0]["name"] = "example.test/platform/other@sha256:" + "a" * 64

    with pytest.raises(AttestationVerificationError, match="subject name"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_raw_image_digest_subject(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    statement = fixture["statements"][
        "example.test/platform/codeprobe-agent@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    ]
    statement["subject"][0]["name"] = (
        "example.test/platform/codeprobe-agent@sha256:"
        "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )

    with pytest.raises(AttestationVerificationError, match="subject name"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_subject_digest_mismatch(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    statement = fixture["statements"][
        "example.test/platform/codeprobe-agent@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    ]
    statement["subject"][0]["digest"]["sha256"] = "b" * 64

    with pytest.raises(AttestationVerificationError, match="subject digest"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_prefixed_subject_digest(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    statement = fixture["statements"][
        "example.test/platform/codeprobe-agent@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    ]
    statement["subject"][0]["digest"]["sha256"] = (
        "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )

    with pytest.raises(AttestationVerificationError, match="subject digest"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_multiple_subjects(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    statement = _statement(fixture)
    statement["subject"].append(copy.deepcopy(statement["subject"][0]))

    with pytest.raises(AttestationVerificationError, match="exactly one subject"):
        _verify(fixture)


@pytest.mark.parametrize(
    ("field_path", "match"),
    [
        (("dataLicense",), "dataLicense"),
        (("documentNamespace",), "documentNamespace"),
        (("creationInfo", "created"), "creationInfo.created"),
    ],
)
def test_verify_buildkit_attestations_rejects_spdx_missing_core_fields(
    attestation_fixture: dict[str, Any], field_path: tuple[str, ...], match: str
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    target = _statement(fixture)["predicate"]
    for field in field_path[:-1]:
        target = target[field]
    del target[field_path[-1]]

    with pytest.raises(AttestationVerificationError, match=match):
        _verify(fixture)


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("spdxVersion", "SPDX-2.2", "spdxVersion"),
        ("SPDXID", "SPDXRef-OTHER", "SPDXID"),
        ("dataLicense", "MIT", "dataLicense"),
    ],
)
def test_verify_buildkit_attestations_rejects_spdx_near_match_values(
    attestation_fixture: dict[str, Any], key: str, value: str, match: str
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    _statement(fixture)["predicate"][key] = value

    with pytest.raises(AttestationVerificationError, match=match):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_non_rfc3339_spdx_created(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    _statement(fixture)["predicate"]["creationInfo"]["created"] = "2026-07-29"

    with pytest.raises(AttestationVerificationError, match="creationInfo.created"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_impossible_rfc3339_spdx_created(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    _statement(fixture)["predicate"]["creationInfo"][
        "created"
    ] = "2026-99-99T25:61:61Z"

    with pytest.raises(AttestationVerificationError, match="creationInfo.created"):
        _verify(fixture)


@pytest.mark.parametrize(
    "namespace",
    ["not a URI", "https://example.test/spdx namespace", "https://example.test/%ZZ"],
)
def test_verify_buildkit_attestations_rejects_invalid_document_namespace(
    attestation_fixture: dict[str, Any], namespace: str
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    _statement(fixture)["predicate"]["documentNamespace"] = namespace

    with pytest.raises(AttestationVerificationError, match="documentNamespace"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_empty_spdx_package_inventory(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    _statement(fixture)["predicate"]["packages"] = []

    with pytest.raises(AttestationVerificationError, match="packages"):
        _verify(fixture)


@pytest.mark.parametrize(("field", "match"), [("files", "files"), ("relationships", "relationships")])
def test_verify_buildkit_attestations_rejects_empty_spdx_usefulness_sections(
    attestation_fixture: dict[str, Any], field: str, match: str
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    _statement(fixture)["predicate"][field] = []

    with pytest.raises(AttestationVerificationError, match=match):
        _verify(fixture)


@pytest.mark.parametrize(
    ("field", "entry_key", "match"),
    [
        ("files", "fileName", "fileName"),
        ("relationships", "relatedSpdxElement", "relatedSpdxElement"),
    ],
)
def test_verify_buildkit_attestations_rejects_spdx_usefulness_skeletons(
    attestation_fixture: dict[str, Any], field: str, entry_key: str, match: str
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    _statement(fixture)["predicate"][field][0][entry_key] = ""

    with pytest.raises(AttestationVerificationError, match=match):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_empty_spdx_package_version_when_present(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    _statement(fixture)["predicate"]["packages"][0]["versionInfo"] = ""

    with pytest.raises(AttestationVerificationError, match="invalid versionInfo"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_duplicate_spdx_ids(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    predicate = _statement(fixture)["predicate"]
    predicate["files"][0]["SPDXID"] = predicate["packages"][0]["SPDXID"]

    with pytest.raises(AttestationVerificationError, match="duplicate SPDXID"):
        _verify(fixture)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("spdxElementId", "SPDXRef-Missing-source"),
        ("relatedSpdxElement", "SPDXRef-Missing-target"),
    ],
)
def test_verify_buildkit_attestations_rejects_unknown_relationship_elements(
    attestation_fixture: dict[str, Any], field: str, value: str
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    _statement(fixture)["predicate"]["relationships"][0][field] = value

    with pytest.raises(AttestationVerificationError, match="unknown SPDX element"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_unknown_relationship_type(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    relationship = _statement(fixture)["predicate"]["relationships"][0]
    relationship["relationshipType"] = "LOOKS_LIKE"

    with pytest.raises(AttestationVerificationError, match="relationshipType"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_spdx_without_creation_info(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    statement = fixture["statements"][
        "example.test/platform/codeprobe-agent@sha256:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    ]
    del statement["predicate"]["creationInfo"]

    with pytest.raises(AttestationVerificationError, match="creationInfo"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_wrong_buildkit_build_type(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    _statement(fixture, AMD64_SLSA_REF)["predicate"]["buildDefinition"][
        "buildType"
    ] = "https://example.test/buildkit"

    with pytest.raises(AttestationVerificationError, match="buildType"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_missing_builder_id_field(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    del _statement(fixture, AMD64_SLSA_REF)["predicate"]["runDetails"]["builder"]["id"]

    with pytest.raises(AttestationVerificationError, match="builder id"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_empty_material_digest_value(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    material = _statement(fixture, AMD64_SLSA_REF)["predicate"]["buildDefinition"][
        "resolvedDependencies"
    ][0]
    material["digest"]["sha1"] = ""

    with pytest.raises(AttestationVerificationError, match="digest value"):
        _verify(fixture)


@pytest.mark.parametrize(
    "uri",
    [
        "not-a-uri",
        "git+https://github.com/source/repo\n",
        "https://example.test/path with spaces",
        "https://example.test/%ZZ",
    ],
)
def test_verify_buildkit_attestations_rejects_malformed_material_uri(
    attestation_fixture: dict[str, Any], uri: str
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    material = _statement(fixture, AMD64_SLSA_REF)["predicate"]["buildDefinition"][
        "resolvedDependencies"
    ][0]
    material["uri"] = uri

    with pytest.raises(AttestationVerificationError, match="invalid uri"):
        _verify(fixture)


@pytest.mark.parametrize(
    "digest",
    [
        {"md5": "0" * 32},
        {"sha1": "0" * 39},
        {"sha256": "z" * 64},
    ],
)
def test_verify_buildkit_attestations_rejects_invalid_material_digest_format(
    attestation_fixture: dict[str, Any], digest: dict[str, str]
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    material = _statement(fixture, AMD64_SLSA_REF)["predicate"]["buildDefinition"][
        "resolvedDependencies"
    ][0]
    material["digest"] = digest

    with pytest.raises(AttestationVerificationError, match="digest"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_slsa_without_materials(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    statement = fixture["statements"][
        "example.test/platform/codeprobe-agent@sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
    ]
    statement["predicate"]["buildDefinition"]["resolvedDependencies"] = []

    with pytest.raises(AttestationVerificationError, match="materials"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_duplicate_platform_manifest(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    duplicate = copy.deepcopy(_index_manifests(fixture)[0])
    duplicate["digest"] = NEW_DIGEST
    _index_manifests(fixture).append(duplicate)

    with pytest.raises(AttestationVerificationError, match="duplicate platform"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_extra_runnable_platform_manifest(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    _index_manifests(fixture).append(
        {
            "digest": NEW_DIGEST,
            "platform": {
                "architecture": "s390x",
                "os": "linux",
            },
        }
    )

    with pytest.raises(AttestationVerificationError, match="unsupported platform"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_duplicate_attestation_manifest(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    duplicate = copy.deepcopy(_index_manifests(fixture)[2])
    duplicate["digest"] = NEW_DIGEST
    _index_manifests(fixture).append(duplicate)

    with pytest.raises(AttestationVerificationError, match="duplicate attestation"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_duplicate_predicate_layer(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    duplicate = copy.deepcopy(_amd64_attestation_layers(fixture)[0])
    duplicate["digest"] = NEW_DIGEST
    _amd64_attestation_layers(fixture).append(duplicate)

    with pytest.raises(AttestationVerificationError, match="duplicate predicate"):
        _verify(fixture)


def test_verify_buildkit_attestations_rejects_extra_predicate_layer(
    attestation_fixture: dict[str, Any],
) -> None:
    fixture = copy.deepcopy(attestation_fixture)
    _amd64_attestation_layers(fixture).append(
        {
            "annotations": {
                "in-toto.io/predicate-type": "https://example.test/predicate"
            },
            "digest": NEW_DIGEST,
        }
    )

    with pytest.raises(AttestationVerificationError, match="unsupported predicate"):
        _verify(fixture)


def test_load_statement_rejects_malformed_gzip_payload() -> None:
    with pytest.raises(AttestationVerificationError, match="malformed gzip"):
        _load_statement(b"\x1f\x8bnot-a-valid-gzip", "sha256:" + "1" * 64)


def test_load_statement_rejects_oversized_compressed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "codeprobe.sandbox.oci_attestations._MAX_COMPRESSED_PAYLOAD_BYTES", 16
    )

    with pytest.raises(AttestationVerificationError, match="size limit"):
        _load_statement(gzip.compress(b'{"value":"small"}'), "sha256:" + "1" * 64)


def test_load_statement_rejects_gzip_expansion_over_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "codeprobe.sandbox.oci_attestations._MAX_COMPRESSED_PAYLOAD_BYTES", 1_024
    )
    monkeypatch.setattr(
        "codeprobe.sandbox.oci_attestations._MAX_DECOMPRESSED_PAYLOAD_BYTES", 64
    )
    payload = gzip.compress(json.dumps({"value": "x" * 1_000}).encode())

    with pytest.raises(AttestationVerificationError, match="size limit"):
        _load_statement(payload, "sha256:" + "1" * 64)


def test_load_statement_rejects_oversized_plain_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "codeprobe.sandbox.oci_attestations._MAX_DECOMPRESSED_PAYLOAD_BYTES", 16
    )

    with pytest.raises(AttestationVerificationError, match="size limit"):
        _load_statement(b'{"value":"too large"}', "sha256:" + "1" * 64)


def test_load_statement_rejects_malformed_utf8_payload() -> None:
    with pytest.raises(AttestationVerificationError, match="malformed UTF-8"):
        _load_statement(b"\xff", "sha256:" + "1" * 64)


def test_load_statement_rejects_malformed_json_payload() -> None:
    with pytest.raises(AttestationVerificationError, match="malformed JSON"):
        _load_statement(gzip.compress(b"{"), "sha256:" + "1" * 64)


def test_docker_manifest_fetch_has_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert kwargs["timeout"] == 120.0
        raise subprocess.TimeoutExpired(cmd=["docker"], timeout=120.0)

    monkeypatch.setattr(subprocess, "run", timeout_run)

    with pytest.raises(AttestationVerificationError, match="timed out"):
        _docker_raw_manifest("example.test/image@sha256:" + "1" * 64)


def test_docker_manifest_fetch_launch_error_is_controlled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def launch_error(*args: object, **kwargs: object) -> object:
        raise FileNotFoundError("docker")

    monkeypatch.setattr(subprocess, "run", launch_error)

    with pytest.raises(AttestationVerificationError, match="failed to launch"):
        _docker_raw_manifest("example.test/image@sha256:" + "1" * 64)


def test_oras_blob_fetch_has_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timeout_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert kwargs["timeout"] == 120.0
        raise subprocess.TimeoutExpired(cmd=["oras"], timeout=120.0)

    monkeypatch.setattr(subprocess, "run", timeout_run)

    with pytest.raises(AttestationVerificationError, match="timed out"):
        _oras_blob_fetch("example.test/image@sha256:" + "1" * 64)
