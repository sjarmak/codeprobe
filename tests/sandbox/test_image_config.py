from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from codeprobe.sandbox.image_config import (
    CONTAINER_CONFIG_ENV,
    PreparedImage,
    PreparedImages,
    container_config_path,
    load_prepared_images,
    write_prepared_images,
)

_DIGEST_A = "sha256:" + "a" * 64
_DIGEST_B = "sha256:" + "b" * 64
_LOCAL_A = "sha256:" + "c" * 64
_LOCAL_B = "sha256:" + "d" * 64


def _prepared() -> PreparedImages:
    return PreparedImages(
        engine="docker",
        agent=PreparedImage(
            source_reference="registry.example/team/codeprobe-agent:0.13.0",
            verified_reference=(f"registry.example/team/codeprobe-agent:0.13.0@{_DIGEST_A}"),
            digest=_DIGEST_A,
            local_id=_LOCAL_A,
        ),
        scoring=PreparedImage(
            source_reference="registry.example/team/codeprobe-scoring:0.13.0",
            verified_reference=(f"registry.example/team/codeprobe-scoring:0.13.0@{_DIGEST_B}"),
            digest=_DIGEST_B,
            local_id=_LOCAL_B,
        ),
    )


def test_config_path_uses_explicit_absolute_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    expected = tmp_path / "container-images.json"
    monkeypatch.setenv(CONTAINER_CONFIG_ENV, str(expected))

    assert container_config_path() == expected


def test_config_path_rejects_relative_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(CONTAINER_CONFIG_ENV, "relative/config.json")

    with pytest.raises(ValueError, match="absolute"):
        container_config_path()


def test_missing_config_is_optional(tmp_path: Path) -> None:
    assert load_prepared_images(tmp_path / "missing.json") is None


def test_write_then_load_round_trip_is_atomic_and_private(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "container-images.json"

    written = write_prepared_images(_prepared(), path)

    assert written == path
    assert load_prepared_images(path) == _prepared()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".container-images.json.*"))


def test_permission_failure_preserves_existing_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "container-images.json"
    original = b"existing-record\n"
    path.write_bytes(original)

    def fail_chmod(_path: Path, _mode: int) -> None:
        raise OSError("fault injected")

    monkeypatch.setattr(Path, "chmod", fail_chmod)

    with pytest.raises(OSError, match="fault injected"):
        write_prepared_images(_prepared(), path)

    assert path.read_bytes() == original
    assert not list(path.parent.glob(".container-images.json.*"))


def test_load_rejects_unknown_fields(tmp_path: Path) -> None:
    path = write_prepared_images(_prepared(), tmp_path / "config.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["unexpected"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="fields"):
        load_prepared_images(path)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("engine", "containerd", "engine"),
        ("agent.digest", "sha512:" + "a" * 128, "digest"),
        ("agent.local_id", "sha256:short", "local image ID"),
        (
            "agent.verified_reference",
            "registry.example/team/codeprobe-agent:0.13.0",
            "digest-pinned",
        ),
    ],
)
def test_load_rejects_malformed_identity_fields(tmp_path: Path, field: str, value: str, message: str) -> None:
    path = write_prepared_images(_prepared(), tmp_path / "config.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    target = payload
    parts = field.split(".")
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_prepared_images(path)


def test_load_rejects_digest_that_disagrees_with_verified_reference(
    tmp_path: Path,
) -> None:
    path = write_prepared_images(_prepared(), tmp_path / "config.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["agent"]["digest"] = _DIGEST_B
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="does not match"):
        load_prepared_images(path)


def test_load_rejects_symlink(tmp_path: Path) -> None:
    target = write_prepared_images(_prepared(), tmp_path / "target.json")
    link = tmp_path / "link.json"
    link.symlink_to(target)

    with pytest.raises(ValueError, match="symlink"):
        load_prepared_images(link)


def test_load_rejects_oversized_config(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_bytes(b" " * 70_000)

    with pytest.raises(ValueError, match="too large"):
        load_prepared_images(path)
