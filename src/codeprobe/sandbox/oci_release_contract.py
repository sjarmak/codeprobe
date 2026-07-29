"""Schema contracts for OCI release-pair authority."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

EXPECTED_IMAGES: Final[tuple[str, ...]] = ("codeprobe-agent", "codeprobe-scoring")
REQUIRED_PLATFORMS: Final[tuple[str, ...]] = ("linux/amd64", "linux/arm64")
_DIGEST_RE: Final[re.Pattern[str]] = re.compile(r"sha256:[a-f0-9]{64}\Z")
_SHA_RE: Final[re.Pattern[str]] = re.compile(r"[a-f0-9]{40}\Z")


class OciReleaseError(RuntimeError):
    """Raised when OCI release authority verification fails."""


@dataclass(frozen=True)
class ImageIdentity:
    image: str
    version: str
    tag_ref: str
    candidate_ref: str
    digest: str
    digest_ref: str
    platforms: tuple[str, ...]
    runtime_override_env: str
    source_sha: str

    @property
    def image_ref(self) -> str:
        return self.digest_ref.rsplit("@", 1)[0]

    def as_json(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "image": self.image,
            "version": self.version,
            "tag_ref": self.tag_ref,
            "candidate_ref": self.candidate_ref,
            "digest": self.digest,
            "digest_ref": self.digest_ref,
            "platforms": list(self.platforms),
            "runtime_override_env": self.runtime_override_env,
            "source_sha": self.source_sha,
        }


def load_identities(identity_dir: Path, *, strict: bool = True) -> tuple[ImageIdentity, ...]:
    try:
        paths = sorted(identity_dir.glob("*.json"))
    except OSError as exc:
        raise OciReleaseError("could not read image identity directory") from exc
    identities = tuple(_identity_from_json(_read_object(path, "image identity")) for path in paths)
    if strict and tuple(item.image for item in identities) != EXPECTED_IMAGES:
        raise OciReleaseError("release pair requires exact agent/scoring identities")
    return identities


def build_pair(
    repository: str, ref: str, source_sha: str, identities: Iterable[ImageIdentity]
) -> dict[str, object]:
    items = sorted(identities, key=lambda item: item.image)
    version = single_version(items)
    return {
        "release_pair_schema": 1,
        "version": version,
        "source": {
            "repository": repository,
            "ref": ref,
            "sha": source_sha,
            "workflow": "publish-images.yml",
        },
        "version_tag_state": "new",
        "images": [item.as_json() for item in items],
    }


def load_pair_identities(
    path: Path, repository: str, ref: str, source_sha: str, version: str
) -> tuple[ImageIdentity, ...]:
    pair = _read_object(path, "release pair")
    if set(pair) != {
        "release_pair_schema",
        "version",
        "source",
        "version_tag_state",
        "images",
    }:
        raise OciReleaseError("release pair schema mismatch")
    _verify_pair_source(pair, repository, ref, source_sha, version)
    images = pair["images"]
    if not isinstance(images, list) or len(images) != len(EXPECTED_IMAGES):
        raise OciReleaseError("release pair must contain exact image identities")
    identities = tuple(_identity_from_json(item) for item in images if isinstance(item, dict))
    if len(identities) != len(images) or tuple(item.image for item in identities) != EXPECTED_IMAGES:
        raise OciReleaseError("release pair image identity mismatch")
    return identities


def validate_identity_contracts(
    identities: Iterable[ImageIdentity],
    *,
    registry: str,
    namespace: str,
    version: str,
    source_sha: str,
) -> None:
    items = tuple(identities)
    if tuple(item.image for item in items) != EXPECTED_IMAGES:
        raise OciReleaseError("release pair requires exact agent/scoring identities")
    for identity in items:
        _validate_identity_contract(identity, registry, namespace, version, source_sha)


def validate_promotion_state(path: Path, identities: Iterable[ImageIdentity]) -> None:
    state = _read_object(path, "promotion state")
    if set(state) != {"promotion_state_schema", "version_tag_state", "promoted"}:
        raise OciReleaseError("promotion state schema mismatch")
    if state["promotion_state_schema"] != 1 or state["version_tag_state"] != "new":
        raise OciReleaseError("promotion state version tag state mismatch")
    expected = {(item.tag_ref, item.digest_ref) for item in identities}
    promoted = state["promoted"]
    if not isinstance(promoted, list) or len(promoted) != len(expected):
        raise OciReleaseError("promotion state promoted list mismatch")
    if {_promoted_pair(item) for item in promoted} != expected:
        raise OciReleaseError("promotion state promoted identities mismatch")


def single_version(identities: Iterable[ImageIdentity]) -> str:
    versions = {identity.version for identity in identities}
    if len(versions) != 1:
        raise OciReleaseError("release pair has inconsistent versions")
    return versions.pop()


def _read_object(path: Path, label: str) -> dict[str, object]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise OciReleaseError(f"could not read {label}") from exc
    except json.JSONDecodeError as exc:
        raise OciReleaseError(f"malformed {label} JSON") from exc
    if not isinstance(loaded, dict):
        raise OciReleaseError(f"{label} must be a JSON object")
    return loaded


def _identity_from_json(data: dict[str, object]) -> ImageIdentity:
    required_keys = set(ImageIdentity.__dataclass_fields__) | {"schema_version"}
    if set(data) != required_keys or data.get("schema_version") != 1:
        raise OciReleaseError("image identity schema mismatch")
    platforms = data["platforms"]
    if platforms != list(REQUIRED_PLATFORMS):
        raise OciReleaseError("image identity platform contract mismatch")
    values = {key: data[key] for key in ImageIdentity.__dataclass_fields__}
    if not all(isinstance(value, str) for key, value in values.items() if key != "platforms"):
        raise OciReleaseError("image identity contains non-string field")
    if not _DIGEST_RE.fullmatch(str(values["digest"])):
        raise OciReleaseError("image identity digest is not sha256")
    values["platforms"] = tuple(platforms)
    return ImageIdentity(**values)  # type: ignore[arg-type]


def _verify_pair_source(
    pair: dict[str, object], repository: str, ref: str, source_sha: str, version: str
) -> None:
    source = pair.get("source")
    if not isinstance(source, dict):
        raise OciReleaseError("release pair source is invalid")
    expected = {
        "repository": repository,
        "ref": ref,
        "sha": source_sha,
        "workflow": "publish-images.yml",
    }
    if pair.get("release_pair_schema") != 1 or pair.get("version") != version:
        raise OciReleaseError("release pair version contract mismatch")
    if pair.get("version_tag_state") != "new":
        raise OciReleaseError("release pair version tag state mismatch")
    if source != expected:
        raise OciReleaseError("release pair source contract mismatch")


def _validate_identity_contract(
    identity: ImageIdentity, registry: str, namespace: str, version: str, source_sha: str
) -> None:
    repo = image_repo(registry, namespace, identity.image)
    if not _SHA_RE.fullmatch(source_sha):
        raise OciReleaseError("release source sha must be a full lowercase sha")
    if identity.version != version or identity.source_sha != source_sha:
        raise OciReleaseError(f"{identity.image} identity source contract mismatch")
    if identity.runtime_override_env != _runtime_override_env(identity.image):
        raise OciReleaseError(f"{identity.image} runtime override mismatch")
    if identity.tag_ref != f"{repo}:{version}":
        raise OciReleaseError(f"{identity.image} official tag ref mismatch")
    if identity.digest_ref != f"{repo}@{identity.digest}":
        raise OciReleaseError(f"{identity.image} digest ref mismatch")
    if not _candidate_ref_matches(identity.candidate_ref, repo, version, source_sha):
        raise OciReleaseError(f"{identity.image} candidate ref mismatch")


def image_repo(registry: str, namespace: str, image: str) -> str:
    return f"{registry}/{namespace}/{image}".lower()


def _runtime_override_env(image: str) -> str:
    if image == "codeprobe-agent":
        return "CODEPROBE_AGENT_IMAGE"
    if image == "codeprobe-scoring":
        return "CODEPROBE_SCORING_IMAGE"
    raise OciReleaseError(f"unsupported image identity {image}")


def _candidate_ref_matches(
    candidate_ref: str, repo: str, version: str, source_sha: str
) -> bool:
    pattern = (
        rf"{re.escape(repo)}:{re.escape(version)}-"
        rf"[0-9]+-[0-9]+-{re.escape(source_sha[:12])}\Z"
    )
    return re.fullmatch(pattern, candidate_ref) is not None


def _promoted_pair(item: object) -> tuple[str, str]:
    if not isinstance(item, dict) or set(item) != {"tag_ref", "digest_ref"}:
        raise OciReleaseError("promotion state promoted entry mismatch")
    tag_ref = item["tag_ref"]
    digest_ref = item["digest_ref"]
    if not isinstance(tag_ref, str) or not isinstance(digest_ref, str):
        raise OciReleaseError("promotion state promoted entry mismatch")
    return tag_ref, digest_ref
