"""Structural validation for BuildKit SPDX and SLSA predicates."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Final, TypeGuard
from urllib.parse import urlsplit

SPDX_PREDICATE_TYPE: Final[str] = "https://spdx.dev/Document"
SLSA_PREDICATE_TYPE: Final[str] = "https://slsa.dev/provenance/v1"
BUILDKIT_BUILD_TYPE: Final[str] = (
    "https://github.com/moby/buildkit/blob/master/docs/attestations/"
    "slsa-definitions.md"
)

_RFC3339_RE: Final[re.Pattern[str]] = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})\Z"
)
_SPDX_ID_RE: Final[re.Pattern[str]] = re.compile(r"SPDXRef-[A-Za-z0-9.-]+\Z")
_URI_SCHEME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*\Z")
_INVALID_PERCENT_ESCAPE_RE: Final[re.Pattern[str]] = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SPDX_RELATIONSHIP_TYPES: Final[frozenset[str]] = frozenset(
    {
        "AMENDS",
        "ANCESTOR_OF",
        "BUILD_DEPENDENCY_OF",
        "BUILD_TOOL_OF",
        "CONTAINED_BY",
        "CONTAINS",
        "COPY_OF",
        "DATA_FILE_OF",
        "DEPENDENCY_MANIFEST_OF",
        "DEPENDENCY_OF",
        "DEPENDS_ON",
        "DESCENDANT_OF",
        "DESCRIBED_BY",
        "DESCRIBES",
        "DEV_DEPENDENCY_OF",
        "DEV_TOOL_OF",
        "DISTRIBUTION_ARTIFACT",
        "DOCUMENTATION_OF",
        "DYNAMIC_LINK",
        "EXAMPLE_OF",
        "EXPANDED_FROM_ARCHIVE",
        "FILE_ADDED",
        "FILE_DELETED",
        "FILE_MODIFIED",
        "GENERATED_FROM",
        "GENERATES",
        "HAS_PREREQUISITE",
        "METAFILE_OF",
        "OPTIONAL_COMPONENT_OF",
        "OPTIONAL_DEPENDENCY_OF",
        "OTHER",
        "PACKAGE_OF",
        "PATCH_APPLIED",
        "PATCH_FOR",
        "PREREQUISITE_FOR",
        "PROVIDED_DEPENDENCY_OF",
        "REQUIREMENT_DESCRIPTION_FOR",
        "RUNTIME_DEPENDENCY_OF",
        "SPECIFICATION_FOR",
        "STATIC_LINK",
        "TEST_CASE_OF",
        "TEST_DEPENDENCY_OF",
        "TEST_OF",
        "TEST_TOOL_OF",
        "VARIANT_OF",
    }
)
_SLSA_DIGEST_LENGTHS: Final[dict[str, int]] = {
    "sha1": 40,
    "sha256": 64,
    "sha384": 96,
    "sha512": 128,
}


class AttestationVerificationError(ValueError):
    """Raised when image attestation payloads fail the release contract."""


def verify_spdx_predicate(predicate: dict[str, Any], layer_digest: str) -> None:
    _verify_spdx_document_fields(predicate, layer_digest)
    _verify_spdx_creation_info(predicate, layer_digest)
    package_ids = _verify_spdx_packages(predicate, layer_digest)
    file_ids = _verify_spdx_files(predicate, layer_digest)
    element_ids = {"SPDXRef-DOCUMENT", *package_ids, *file_ids}
    expected_count = 1 + len(package_ids) + len(file_ids)
    if len(element_ids) != expected_count:
        raise AttestationVerificationError(
            f"{layer_digest} SPDX predicate has duplicate SPDXID"
        )
    _verify_spdx_relationships(predicate, layer_digest, element_ids)


def _verify_spdx_document_fields(
    predicate: dict[str, Any], layer_digest: str
) -> None:
    expected = {
        "spdxVersion": "SPDX-2.3",
        "SPDXID": "SPDXRef-DOCUMENT",
        "dataLicense": "CC0-1.0",
    }
    for key, value in expected.items():
        if predicate.get(key) != value:
            raise AttestationVerificationError(
                f"{layer_digest} SPDX predicate has invalid {key}"
            )
    if not isinstance(predicate.get("name"), str) or not predicate["name"]:
        raise AttestationVerificationError(
            f"{layer_digest} SPDX predicate missing name"
        )
    namespace = predicate.get("documentNamespace")
    if not _is_valid_absolute_uri(namespace):
        raise AttestationVerificationError(
            f"{layer_digest} SPDX predicate has invalid documentNamespace"
        )


def _verify_spdx_creation_info(
    predicate: dict[str, Any], layer_digest: str
) -> None:
    creation_info = predicate.get("creationInfo")
    if not isinstance(creation_info, dict):
        raise AttestationVerificationError(
            f"{layer_digest} SPDX predicate missing creationInfo"
        )
    creators = creation_info.get("creators")
    created = creation_info.get("created")
    if not _is_valid_rfc3339(created):
        raise AttestationVerificationError(
            f"{layer_digest} SPDX predicate has invalid creationInfo.created"
        )
    if (
        not isinstance(creators, list)
        or not creators
        or not all(isinstance(creator, str) and creator for creator in creators)
    ):
        raise AttestationVerificationError(
            f"{layer_digest} SPDX predicate missing creators"
        )


def _verify_spdx_packages(
    predicate: dict[str, Any], layer_digest: str
) -> tuple[str, ...]:
    packages = predicate.get("packages")
    if not isinstance(packages, list) or not packages:
        raise AttestationVerificationError(
            f"{layer_digest} SPDX predicate missing packages"
        )
    package_ids: list[str] = []
    for package in packages:
        if not isinstance(package, dict):
            raise AttestationVerificationError(
                f"{layer_digest} SPDX package is invalid"
            )
        package_ids.append(_require_spdx_id(package, layer_digest))
        if not isinstance(package.get("name"), str) or not package["name"]:
            raise AttestationVerificationError(
                f"{layer_digest} SPDX package missing name"
            )
        version_info = package.get("versionInfo")
        if version_info is not None and (
            not isinstance(version_info, str) or not version_info
        ):
            raise AttestationVerificationError(
                f"{layer_digest} SPDX package has invalid versionInfo"
            )
    return tuple(package_ids)


def _verify_spdx_files(
    predicate: dict[str, Any], layer_digest: str
) -> tuple[str, ...]:
    files = predicate.get("files")
    if not isinstance(files, list) or not files:
        raise AttestationVerificationError(
            f"{layer_digest} SPDX predicate missing files"
        )
    file_ids: list[str] = []
    for file_entry in files:
        _require_spdx_object_fields(file_entry, ("SPDXID", "fileName"), layer_digest)
        assert isinstance(file_entry, dict)
        file_ids.append(_require_spdx_id(file_entry, layer_digest))
    return tuple(file_ids)


def _verify_spdx_relationships(
    predicate: dict[str, Any], layer_digest: str, element_ids: set[str]
) -> None:
    relationships = predicate.get("relationships")
    if not isinstance(relationships, list) or not relationships:
        raise AttestationVerificationError(
            f"{layer_digest} SPDX predicate missing relationships"
        )
    for relationship in relationships:
        _require_spdx_object_fields(
            relationship,
            ("spdxElementId", "relationshipType", "relatedSpdxElement"),
            layer_digest,
        )
        assert isinstance(relationship, dict)
        if relationship["relationshipType"] not in _SPDX_RELATIONSHIP_TYPES:
            raise AttestationVerificationError(
                f"{layer_digest} SPDX object has invalid relationshipType"
            )
        for key in ("spdxElementId", "relatedSpdxElement"):
            if relationship[key] not in element_ids:
                raise AttestationVerificationError(
                    f"{layer_digest} SPDX relationship references unknown SPDX element"
                )


def _require_spdx_id(entry: dict[str, Any], layer_digest: str) -> str:
    spdx_id = entry.get("SPDXID")
    if not isinstance(spdx_id, str) or _SPDX_ID_RE.fullmatch(spdx_id) is None:
        raise AttestationVerificationError(
            f"{layer_digest} SPDX object has invalid SPDXID"
        )
    return spdx_id


def _require_spdx_object_fields(
    entry: object, keys: tuple[str, ...], layer_digest: str
) -> None:
    if not isinstance(entry, dict):
        raise AttestationVerificationError(f"{layer_digest} SPDX object is invalid")
    for key in keys:
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise AttestationVerificationError(
                f"{layer_digest} SPDX object missing {key}"
            )


def verify_slsa_predicate(predicate: dict[str, Any], layer_digest: str) -> None:
    build_type, builder, materials = _slsa_metadata(predicate)
    if build_type != BUILDKIT_BUILD_TYPE:
        raise AttestationVerificationError(
            f"{layer_digest} provenance predicate has unexpected buildType"
        )
    if builder is None:
        raise AttestationVerificationError(
            f"{layer_digest} provenance predicate missing builder id"
        )
    if not isinstance(materials, list) or not materials:
        raise AttestationVerificationError(
            f"{layer_digest} provenance predicate missing materials"
        )
    for material in materials:
        _verify_slsa_material(material, layer_digest)


def _slsa_metadata(
    predicate: dict[str, Any],
) -> tuple[str | None, str | None, object]:
    build_type = _string_at(predicate, ("buildType",)) or _string_at(
        predicate, ("buildDefinition", "buildType")
    )
    builder = _string_at(predicate, ("builder", "id"), allow_empty=True)
    if builder is None:
        builder = _string_at(predicate, ("runDetails", "builder", "id"), allow_empty=True)
    materials = predicate.get("materials")
    if materials is None:
        build_definition = predicate.get("buildDefinition")
        if isinstance(build_definition, dict):
            materials = build_definition.get("resolvedDependencies")
    return build_type, builder, materials


def _verify_slsa_material(material: object, layer_digest: str) -> None:
    if not isinstance(material, dict):
        raise AttestationVerificationError(
            f"{layer_digest} provenance material is invalid"
        )
    if not _is_valid_material_uri(material.get("uri")):
        raise AttestationVerificationError(
            f"{layer_digest} provenance material has invalid uri"
        )
    digest = material.get("digest")
    if not isinstance(digest, dict) or not digest:
        raise AttestationVerificationError(
            f"{layer_digest} provenance material missing digest"
        )
    for algorithm, value in digest.items():
        expected_length = (
            _SLSA_DIGEST_LENGTHS.get(algorithm)
            if isinstance(algorithm, str)
            else None
        )
        if not _is_valid_digest(value, expected_length):
            raise AttestationVerificationError(
                f"{layer_digest} provenance material has invalid digest value"
            )


def _is_valid_digest(value: object, expected_length: int | None) -> bool:
    return (
        expected_length is not None
        and isinstance(value, str)
        and len(value) == expected_length
        and all(character in "0123456789abcdefABCDEF" for character in value)
    )


def _is_valid_material_uri(value: object) -> TypeGuard[str]:
    return _is_valid_absolute_uri(value)


def _is_valid_rfc3339(value: object) -> TypeGuard[str]:
    if not isinstance(value, str) or _RFC3339_RE.fullmatch(value) is None:
        return False
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _is_valid_absolute_uri(value: object) -> TypeGuard[str]:
    if not isinstance(value, str) or not value:
        return False
    if any(ord(character) <= 32 or ord(character) >= 127 for character in value):
        return False
    if _INVALID_PERCENT_ESCAPE_RE.search(value) is not None:
        return False
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError:
        return False
    if _URI_SCHEME_RE.fullmatch(parsed.scheme) is None:
        return False
    if parsed.netloc and hostname is None:
        return False
    if parsed.scheme in {"http", "https", "git+http", "git+https"}:
        return bool(parsed.netloc and hostname)
    return bool(parsed.netloc or parsed.path)


def _string_at(
    source: dict[str, Any], path: tuple[str, ...], *, allow_empty: bool = False
) -> str | None:
    current: object = source
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    if not isinstance(current, str):
        return None
    if current or allow_empty:
        return current
    return None
