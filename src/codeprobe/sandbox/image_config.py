"""Persistent identity record for locally prepared containment images."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Literal

from codeprobe.sandbox.oci_references import (
    DIGEST_PATTERN,
    LOCAL_IMAGE_ID_PATTERN,
    validate_image_reference,
)

CONTAINER_CONFIG_ENV: Final[str] = "CODEPROBE_CONTAINER_CONFIG"
_SCHEMA_VERSION: Final[int] = 1
_MAX_CONFIG_BYTES: Final[int] = 65_536
_ROOT_FIELDS: Final[frozenset[str]] = frozenset({"schema_version", "engine", "agent", "scoring"})
_IMAGE_FIELDS: Final[frozenset[str]] = frozenset({"source_reference", "verified_reference", "digest", "local_id"})

EngineName = Literal["docker", "podman"]


@dataclass(frozen=True)
class PreparedImage:
    source_reference: str
    verified_reference: str
    digest: str
    local_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source_reference": self.source_reference,
            "verified_reference": self.verified_reference,
            "digest": self.digest,
            "local_id": self.local_id,
        }


@dataclass(frozen=True)
class PreparedImages:
    engine: EngineName
    agent: PreparedImage
    scoring: PreparedImage

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "engine": self.engine,
            "agent": self.agent.to_dict(),
            "scoring": self.scoring.to_dict(),
        }


def container_config_path() -> Path:
    override = os.environ.get(CONTAINER_CONFIG_ENV)
    if override is None:
        return Path.home() / ".codeprobe" / "container-images.json"
    path = Path(override).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{CONTAINER_CONFIG_ENV} must be an absolute path")
    return path


def load_prepared_images(path: Path | None = None) -> PreparedImages | None:
    target = path or container_config_path()
    if not target.exists() and not target.is_symlink():
        return None
    if target.is_symlink():
        raise ValueError(f"prepared image config must not be a symlink: {target}")
    payload = _read_payload(target)
    return _parse_prepared_images(payload)


def write_prepared_images(prepared: PreparedImages, path: Path | None = None) -> Path:
    target = path or container_config_path()
    _parse_prepared_images(prepared.to_dict())
    if target.is_symlink():
        raise ValueError(f"prepared image config must not be a symlink: {target}")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    encoded = (json.dumps(prepared.to_dict(), indent=2, sort_keys=True) + "\n").encode()
    temp_path = _write_temp_file(target, encoded)
    try:
        temp_path.chmod(0o600)
        os.replace(temp_path, target)
    except BaseException:
        _unlink_if_present(temp_path)
        raise
    return target


def _read_payload(path: Path) -> object:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"cannot stat prepared image config: {path}") from exc
    if size > _MAX_CONFIG_BYTES:
        raise ValueError(f"prepared image config is too large: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"prepared image config is not valid UTF-8 JSON: {path}") from exc


def _parse_prepared_images(payload: object) -> PreparedImages:
    root = _mapping(payload, "prepared image config")
    _require_fields(root, _ROOT_FIELDS, "prepared image config")
    if root["schema_version"] != _SCHEMA_VERSION:
        raise ValueError("prepared image config has an unsupported schema version")
    engine = root["engine"]
    if engine not in ("docker", "podman"):
        raise ValueError("prepared image config has an invalid engine")
    return PreparedImages(
        engine=engine,
        agent=_parse_image(root["agent"], "agent"),
        scoring=_parse_image(root["scoring"], "scoring"),
    )


def _parse_image(payload: object, label: str) -> PreparedImage:
    image = _mapping(payload, f"{label} image identity")
    _require_fields(image, _IMAGE_FIELDS, f"{label} image identity")
    source = _text(image["source_reference"], f"{label} source reference")
    verified = _text(image["verified_reference"], f"{label} verified reference")
    digest = _text(image["digest"], f"{label} digest")
    local_id = _text(image["local_id"], f"{label} local image ID")
    validate_image_reference(f"{label} source reference", source)
    validate_image_reference(f"{label} verified reference", verified)
    _validate_pins(label, verified, digest, local_id)
    return PreparedImage(source, verified, digest, local_id)


def _validate_pins(label: str, verified: str, digest: str, local_id: str) -> None:
    if DIGEST_PATTERN.fullmatch(digest) is None:
        raise ValueError(f"{label} digest must be a sha256 digest")
    if "@" not in verified:
        raise ValueError(f"{label} verified reference must be digest-pinned")
    if verified.rsplit("@", 1)[1] != digest:
        raise ValueError(f"{label} digest does not match its verified reference")
    if LOCAL_IMAGE_ID_PATTERN.fullmatch(local_id) is None:
        raise ValueError(f"{label} local image ID must be a sha256 image ID")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _require_fields(payload: Mapping[str, object], expected: frozenset[str], label: str) -> None:
    if set(payload) != expected:
        raise ValueError(f"{label} has invalid fields")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _write_temp_file(target: Path, content: bytes) -> Path:
    fd, raw_path = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp")
    temp_path = Path(raw_path)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        _unlink_if_present(temp_path)
        raise
    return temp_path


def _unlink_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
