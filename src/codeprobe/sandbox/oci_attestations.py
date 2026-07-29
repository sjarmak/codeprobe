"""Verify BuildKit OCI attestation payloads for release images."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final, TypeGuard, cast
from urllib.parse import quote, quote_plus

from docker_image import reference as oci_reference  # type: ignore[import-untyped]

from codeprobe.sandbox.oci_predicates import (
    SLSA_PREDICATE_TYPE,
    SPDX_PREDICATE_TYPE,
    verify_slsa_predicate,
    verify_spdx_predicate,
)
from codeprobe.sandbox.oci_predicates import (
    AttestationVerificationError as AttestationVerificationError,
)

IN_TOTO_STATEMENT_TYPE: Final[str] = "https://in-toto.io/Statement/v1"
REQUIRED_PREDICATE_TYPES: Final[frozenset[str]] = frozenset(
    {SPDX_PREDICATE_TYPE, SLSA_PREDICATE_TYPE}
)
REQUIRED_PLATFORMS: Final[tuple[str, ...]] = ("linux/amd64", "linux/arm64")
OCI_INSPECT_TIMEOUT_SECONDS: Final[float] = 120.0
OCI_BLOB_FETCH_TIMEOUT_SECONDS: Final[float] = 120.0
_MAX_COMPRESSED_PAYLOAD_BYTES: Final[int] = 16 * 1024 * 1024
_MAX_DECOMPRESSED_PAYLOAD_BYTES: Final[int] = 64 * 1024 * 1024
_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"sha256:[a-f0-9]{64}\Z")
_DOCKER_IO_LIBRARY_PREFIX: Final[str] = "docker.io/library/"
_DOCKER_IO_PREFIX: Final[str] = "docker.io/"


@dataclass(frozen=True)
class _PlatformAttestation:
    platform: str
    platform_digest: str
    attestation_digest: str


def verify_buildkit_attestations(
    *,
    image_ref: str,
    candidate_ref: str,
    digest_ref: str,
    raw_manifest: Callable[[str], dict[str, Any]],
    blob_fetch: Callable[[str], bytes],
    required_platforms: tuple[str, ...] = REQUIRED_PLATFORMS,
) -> None:
    """Verify SBOM/provenance attestations for every required platform."""

    attestations = _collect_index_contract(
        raw_manifest(digest_ref), digest_ref, required_platforms
    )
    for attestation in attestations:
        _verify_platform_attestation(
            image_ref=image_ref,
            candidate_ref=candidate_ref,
            attestation=attestation,
            raw_manifest=raw_manifest,
            blob_fetch=blob_fetch,
        )


def _collect_index_contract(
    index: dict[str, Any], digest_ref: str, required_platforms: tuple[str, ...]
) -> tuple[_PlatformAttestation, ...]:
    platform_to_digest: dict[str, str] = {}
    digest_to_platform: dict[str, str] = {}
    attestation_manifests: list[tuple[str, str]] = []
    manifests = _required_list(index, "manifests", digest_ref)
    for descriptor in manifests:
        _collect_index_descriptor(
            descriptor=descriptor,
            digest_ref=digest_ref,
            required_platforms=required_platforms,
            platform_to_digest=platform_to_digest,
            digest_to_platform=digest_to_platform,
            attestation_manifests=attestation_manifests,
        )
    _require_complete_platforms(platform_to_digest, required_platforms)
    return _match_attestations(
        attestation_manifests, digest_to_platform, platform_to_digest, required_platforms
    )


def _collect_index_descriptor(
    *,
    descriptor: object,
    digest_ref: str,
    required_platforms: tuple[str, ...],
    platform_to_digest: dict[str, str],
    digest_to_platform: dict[str, str],
    attestation_manifests: list[tuple[str, str]],
) -> None:
    if not isinstance(descriptor, dict):
        raise AttestationVerificationError(
            f"{digest_ref} has invalid manifest descriptor"
        )
    digest = descriptor.get("digest")
    if not _is_sha256_digest(digest):
        raise AttestationVerificationError(
            f"{digest_ref} has manifest descriptor without sha256 digest"
        )
    annotations = descriptor.get("annotations") or {}
    if _is_attestation_descriptor(annotations):
        _record_attestation_descriptor(digest, annotations, attestation_manifests)
        return
    platform = descriptor.get("platform")
    if not isinstance(platform, dict):
        raise AttestationVerificationError(
            f"{digest_ref} has manifest descriptor without platform"
        )
    _record_platform_descriptor(
        digest, platform, digest_ref, required_platforms, platform_to_digest,
        digest_to_platform
    )


def _is_attestation_descriptor(annotations: object) -> TypeGuard[dict[str, Any]]:
    return (
        isinstance(annotations, dict)
        and annotations.get("vnd.docker.reference.type") == "attestation-manifest"
    )


def _record_attestation_descriptor(
    digest: str,
    annotations: dict[str, Any],
    attestation_manifests: list[tuple[str, str]],
) -> None:
    subject = annotations.get("vnd.docker.reference.digest")
    if not _is_sha256_digest(subject):
        raise AttestationVerificationError(
            f"attestation manifest {digest} is missing a sha256 subject"
        )
    attestation_manifests.append((digest, subject))


def _record_platform_descriptor(
    digest: str,
    platform: dict[str, object],
    digest_ref: str,
    required_platforms: tuple[str, ...],
    platform_to_digest: dict[str, str],
    digest_to_platform: dict[str, str],
) -> None:
    key = _platform_key(platform)
    if key not in required_platforms:
        raise AttestationVerificationError(
            f"{digest_ref} has unsupported platform manifest {key}"
        )
    if key in platform_to_digest:
        raise AttestationVerificationError(
            f"{digest_ref} has duplicate platform manifest {key}"
        )
    if digest in digest_to_platform:
        raise AttestationVerificationError(f"{digest_ref} reuses platform digest {digest}")
    platform_to_digest[key] = digest
    digest_to_platform[digest] = key


def _require_complete_platforms(
    platform_to_digest: dict[str, str], required_platforms: tuple[str, ...]
) -> None:
    missing = set(required_platforms) - set(platform_to_digest)
    if missing:
        raise AttestationVerificationError(
            f"missing platform manifests: {sorted(missing)}"
        )


def _match_attestations(
    attestation_manifests: list[tuple[str, str]],
    digest_to_platform: dict[str, str],
    platform_to_digest: dict[str, str],
    required_platforms: tuple[str, ...],
) -> tuple[_PlatformAttestation, ...]:
    attestations_by_platform: dict[str, tuple[str, str]] = {}
    for attestation_digest, subject_digest in attestation_manifests:
        platform = digest_to_platform.get(subject_digest)
        if platform is None:
            raise AttestationVerificationError(
                f"attestation {attestation_digest} targets unknown subject {subject_digest}"
            )
        if platform in attestations_by_platform:
            raise AttestationVerificationError(
                f"duplicate attestation manifest for {platform}"
            )
        attestations_by_platform[platform] = (attestation_digest, subject_digest)

    missing_attestations = set(required_platforms) - set(attestations_by_platform)
    if missing_attestations:
        raise AttestationVerificationError(
            f"missing attestation manifests: {sorted(missing_attestations)}"
        )
    return tuple(
        _PlatformAttestation(
            platform=platform,
            platform_digest=platform_to_digest[platform],
            attestation_digest=attestations_by_platform[platform][0],
        )
        for platform in required_platforms
    )


def _verify_platform_attestation(
    *,
    image_ref: str,
    candidate_ref: str,
    attestation: _PlatformAttestation,
    raw_manifest: Callable[[str], dict[str, Any]],
    blob_fetch: Callable[[str], bytes],
) -> None:
    manifest = raw_manifest(f"{image_ref}@{attestation.attestation_digest}")
    _verify_manifest_subject(
        manifest, attestation.platform_digest, attestation.attestation_digest
    )
    found = _verify_attestation_layers(
        layers=_required_list(manifest, "layers", attestation.attestation_digest),
        image_ref=image_ref,
        candidate_ref=candidate_ref,
        attestation=attestation,
        blob_fetch=blob_fetch,
    )
    if found != REQUIRED_PREDICATE_TYPES:
        missing = REQUIRED_PREDICATE_TYPES - found
        raise AttestationVerificationError(
            f"{attestation.platform} missing predicates: {sorted(missing)}"
        )


def _verify_attestation_layers(
    *,
    layers: list[Any],
    image_ref: str,
    candidate_ref: str,
    attestation: _PlatformAttestation,
    blob_fetch: Callable[[str], bytes],
) -> set[str]:
    found: set[str] = set()
    for layer in layers:
        predicate, layer_digest = _read_layer_descriptor(
            layer, attestation.attestation_digest, found
        )
        statement = _load_statement(blob_fetch(f"{image_ref}@{layer_digest}"), layer_digest)
        _verify_statement(
            statement=statement,
            descriptor_predicate=predicate,
            expected_subject_name=_buildkit_subject_purl(
                candidate_ref, attestation.platform
            ),
            expected_subject_digest=attestation.platform_digest,
            layer_digest=layer_digest,
        )
        found.add(predicate)
    return found


def _read_layer_descriptor(
    layer: object, attestation_digest: str, found_predicates: set[str]
) -> tuple[str, str]:
    if not isinstance(layer, dict):
        raise AttestationVerificationError(
            f"attestation {attestation_digest} has invalid layer"
        )
    annotations = layer.get("annotations") or {}
    if not isinstance(annotations, dict):
        raise AttestationVerificationError(
            f"attestation {attestation_digest} has layer without annotations"
        )
    predicate = annotations.get("in-toto.io/predicate-type")
    layer_digest = layer.get("digest")
    if predicate not in REQUIRED_PREDICATE_TYPES:
        raise AttestationVerificationError(
            f"attestation {attestation_digest} has unsupported predicate {predicate}"
        )
    if predicate in found_predicates:
        raise AttestationVerificationError(
            f"attestation {attestation_digest} has duplicate predicate {predicate}"
        )
    if not _is_sha256_digest(layer_digest):
        raise AttestationVerificationError(
            f"attestation {attestation_digest} has layer without sha256 digest"
        )
    return predicate, layer_digest


def _verify_manifest_subject(
    attestation: dict[str, Any], subject_digest: str, attestation_digest: str
) -> None:
    subject = attestation.get("subject") or {}
    if not isinstance(subject, dict):
        raise AttestationVerificationError(
            f"attestation {attestation_digest} has invalid subject"
        )
    manifest_subject = subject.get("digest")
    if manifest_subject is not None and manifest_subject != subject_digest:
        raise AttestationVerificationError(
            f"attestation {attestation_digest} subject {manifest_subject} != {subject_digest}"
        )


def _load_statement(payload: bytes, layer_digest: str) -> dict[str, Any]:
    if payload.startswith(b"\x1f\x8b"):
        payload = _bounded_gzip_decompress(payload, layer_digest)
    elif len(payload) > _MAX_DECOMPRESSED_PAYLOAD_BYTES:
        raise AttestationVerificationError(
            f"{layer_digest} attestation payload exceeds size limit"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AttestationVerificationError(
            f"{layer_digest} has malformed UTF-8 payload"
        ) from exc
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AttestationVerificationError(
            f"{layer_digest} has malformed JSON payload"
        ) from exc
    if not isinstance(loaded, dict):
        raise AttestationVerificationError(
            f"{layer_digest} did not contain a JSON object"
        )
    return loaded


def _bounded_gzip_decompress(payload: bytes, layer_digest: str) -> bytes:
    if len(payload) > _MAX_COMPRESSED_PAYLOAD_BYTES:
        raise AttestationVerificationError(
            f"{layer_digest} attestation payload exceeds size limit"
        )
    decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        decoded = decompressor.decompress(
            payload, _MAX_DECOMPRESSED_PAYLOAD_BYTES + 1
        )
        if len(decoded) <= _MAX_DECOMPRESSED_PAYLOAD_BYTES:
            decoded += decompressor.flush(
                _MAX_DECOMPRESSED_PAYLOAD_BYTES - len(decoded) + 1
            )
    except zlib.error as exc:
        raise AttestationVerificationError(
            f"{layer_digest} has malformed gzip payload"
        ) from exc
    if len(decoded) > _MAX_DECOMPRESSED_PAYLOAD_BYTES:
        raise AttestationVerificationError(
            f"{layer_digest} attestation payload exceeds size limit"
        )
    if not decompressor.eof or decompressor.unused_data:
        raise AttestationVerificationError(
            f"{layer_digest} has malformed gzip payload"
        )
    return decoded


def _verify_statement(
    *,
    statement: dict[str, Any],
    descriptor_predicate: str,
    expected_subject_name: str,
    expected_subject_digest: str,
    layer_digest: str,
) -> None:
    if statement.get("_type") != IN_TOTO_STATEMENT_TYPE:
        raise AttestationVerificationError(
            f"{layer_digest} is not an in-toto statement"
        )
    if statement.get("predicateType") != descriptor_predicate:
        raise AttestationVerificationError(
            f"{layer_digest} predicate mismatch: {statement.get('predicateType')}"
        )
    subjects = statement.get("subject")
    if not isinstance(subjects, list) or len(subjects) != 1:
        raise AttestationVerificationError(
            f"{layer_digest} must have exactly one subject"
        )
    _verify_statement_subject(
        subject=subjects[0],
        expected_subject_name=expected_subject_name,
        expected_subject_digest=expected_subject_digest,
        layer_digest=layer_digest,
    )
    predicate = statement.get("predicate")
    if not isinstance(predicate, dict):
        raise AttestationVerificationError(f"{layer_digest} is missing predicate")
    if descriptor_predicate == SPDX_PREDICATE_TYPE:
        verify_spdx_predicate(predicate, layer_digest)
    elif descriptor_predicate == SLSA_PREDICATE_TYPE:
        verify_slsa_predicate(predicate, layer_digest)
    else:
        raise AttestationVerificationError(
            f"{layer_digest} has unsupported predicate {descriptor_predicate}"
        )


def _verify_statement_subject(
    *,
    subject: object,
    expected_subject_name: str,
    expected_subject_digest: str,
    layer_digest: str,
) -> None:
    if not isinstance(subject, dict):
        raise AttestationVerificationError(f"{layer_digest} has invalid subject")
    digest = subject.get("digest")
    name = subject.get("name")
    expected_hex = expected_subject_digest.split(":", 1)[1]
    if not isinstance(digest, dict) or digest.get("sha256") != expected_hex:
        raise AttestationVerificationError(
            f"{layer_digest} subject digest does not match {expected_subject_digest}"
        )
    if name != expected_subject_name:
        raise AttestationVerificationError(
            f"{layer_digest} subject name {name!r} does not match platform identity"
        )


def _buildkit_subject_purl(candidate_ref: str, platform: str) -> str:
    try:
        parsed = oci_reference.Reference.parse(candidate_ref)
    except oci_reference.InvalidReference as exc:
        raise AttestationVerificationError(
            f"candidate ref has invalid image reference: {candidate_ref}"
        ) from exc

    name = parsed.get("name")
    tag = parsed.get("tag")
    digest = parsed.get("digest")
    if not isinstance(name, str) or not isinstance(tag, str) or digest is not None:
        raise AttestationVerificationError(
            "candidate ref must be the exact run-unique tag used by BuildKit"
        )

    familiar_name = name
    if familiar_name.startswith(_DOCKER_IO_LIBRARY_PREFIX):
        familiar_name = familiar_name[len(_DOCKER_IO_LIBRARY_PREFIX) :]
    elif familiar_name.startswith(_DOCKER_IO_PREFIX):
        familiar_name = familiar_name[len(_DOCKER_IO_PREFIX) :]
    encoded_name = "/".join(
        quote_plus(segment, safe="") for segment in familiar_name.split("/")
    )
    return (
        f"pkg:docker/{encoded_name}@{quote(tag, safe='')}"
        f"?platform={quote(platform, safe='')}"
    )


def _platform_key(platform: dict[str, object]) -> str:
    os_name = platform.get("os")
    architecture = platform.get("architecture")
    if not isinstance(os_name, str) or not isinstance(architecture, str):
        raise AttestationVerificationError("platform descriptor is missing os/architecture")
    return f"{os_name}/{architecture}"


def _required_list(source: dict[str, Any], key: str, ref: str) -> list[Any]:
    value = source.get(key)
    if not isinstance(value, list):
        raise AttestationVerificationError(f"{ref} has invalid {key}")
    return value


def _is_sha256_digest(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _DIGEST_RE.fullmatch(value) is not None


def _docker_raw_manifest(ref: str) -> dict[str, Any]:
    stdout = _run_text_command(
        ["docker", "buildx", "imagetools", "inspect", "--raw", ref],
        label="docker manifest inspect",
        timeout=OCI_INSPECT_TIMEOUT_SECONDS,
    )
    try:
        loaded = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise AttestationVerificationError(
            f"{ref} returned malformed manifest JSON"
        ) from exc
    if not isinstance(loaded, dict):
        raise AttestationVerificationError(f"{ref} did not resolve to a JSON object")
    return loaded


def _oras_blob_fetch(ref: str) -> bytes:
    return _run_bytes_command(
        ["oras", "blob", "fetch", "--output", "-", ref],
        label="oras blob fetch",
        timeout=OCI_BLOB_FETCH_TIMEOUT_SECONDS,
    )


def _run_text_command(command: list[str], *, label: str, timeout: float) -> str:
    stdout = _run_command(command, label=label, timeout=timeout, text=True)
    if not isinstance(stdout, str):
        raise AttestationVerificationError(f"{label} returned non-text stdout")
    return stdout


def _run_bytes_command(command: list[str], *, label: str, timeout: float) -> bytes:
    stdout = _run_command(command, label=label, timeout=timeout, text=False)
    if not isinstance(stdout, bytes):
        raise AttestationVerificationError(f"{label} returned non-bytes stdout")
    return stdout


def _run_command(
    command: list[str], *, label: str, timeout: float, text: bool
) -> str | bytes:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AttestationVerificationError(
            f"{label} timed out after {timeout:g}s"
        ) from exc
    except OSError as exc:
        raise AttestationVerificationError(f"{label} failed to launch") from exc
    if completed.returncode != 0:
        raise AttestationVerificationError(
            f"{label} failed with exit {completed.returncode}"
        )
    return cast(str | bytes, completed.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify BuildKit SBOM and provenance attestation payloads."
    )
    parser.add_argument("--image-ref", required=True)
    parser.add_argument("--candidate-ref", required=True)
    parser.add_argument("--digest-ref", required=True)
    args = parser.parse_args(argv)

    try:
        verify_buildkit_attestations(
            image_ref=args.image_ref,
            candidate_ref=args.candidate_ref,
            digest_ref=args.digest_ref,
            raw_manifest=_docker_raw_manifest,
            blob_fetch=_oras_blob_fetch,
        )
    except AttestationVerificationError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
