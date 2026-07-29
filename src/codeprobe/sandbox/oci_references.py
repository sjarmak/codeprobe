"""Provider-neutral OCI image reference validation."""

from __future__ import annotations

import re
from typing import Final

from docker_image import reference as oci_reference  # type: ignore[import-untyped]

IMAGE_TAG_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}\Z"
)
DIGEST_PATTERN: Final[re.Pattern[str]] = re.compile(r"sha256:[a-f0-9]{64}\Z")


def is_qualified_registry_host(host: str) -> bool:
    """Return True when Docker will treat *host* as a registry host."""
    return host == "localhost" or "." in host or ":" in host


def validate_tag(name: str, tag: str) -> None:
    if tag == "latest":
        raise ValueError(f"{name} must not use the mutable latest tag")
    if IMAGE_TAG_PATTERN.fullmatch(tag) is None:
        raise ValueError(f"{name} has an invalid image tag")


def validate_image_reference(name: str, reference: str) -> str:
    if "://" in reference:
        raise ValueError(f"{name} must be an OCI image reference, not a URL")
    _require_qualified_image_reference(name, reference)
    if "@" in reference:
        digest_candidate = reference.rsplit("@", 1)[1]
        if (
            digest_candidate.startswith("sha256:")
            and DIGEST_PATTERN.fullmatch(digest_candidate) is None
        ):
            raise ValueError(f"{name} must use a sha256 digest when pinned")
    try:
        parsed = oci_reference.Reference.parse(reference)
    except oci_reference.InvalidReference as exc:
        raise ValueError(f"{name} has an invalid image reference") from exc
    _validate_parsed_reference(name, parsed)
    return reference


def _require_qualified_image_reference(name: str, reference: str) -> None:
    ref_without_digest = reference.split("@", 1)[0]
    if "/" not in ref_without_digest:
        raise ValueError(f"{name} must be a fully qualified OCI image reference")
    host = ref_without_digest.split("/", 1)[0]
    if not is_qualified_registry_host(host):
        raise ValueError(f"{name} must be a fully qualified OCI image reference")


def _validate_parsed_reference(name: str, parsed: object) -> None:
    tag = parsed.get("tag")  # type: ignore[attr-defined]
    digest = parsed.get("digest")  # type: ignore[attr-defined]
    if not isinstance(tag, str) and not isinstance(digest, str):
        raise ValueError(f"{name} must include an explicit tag or digest")
    if isinstance(tag, str):
        validate_tag(name, tag)
    if isinstance(digest, str) and DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError(f"{name} must use a sha256 digest when pinned")
