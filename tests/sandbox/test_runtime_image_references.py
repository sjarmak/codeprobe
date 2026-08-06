"""Tests for provider-neutral runtime OCI image references."""

from __future__ import annotations

from importlib.metadata import version as package_version
from pathlib import Path

import pytest

from codeprobe.sandbox import runner as sandbox_runner
from codeprobe.sandbox.agent_container import containerize_argv
from codeprobe.sandbox.image_config import (
    CONTAINER_CONFIG_ENV,
    PreparedImage,
    PreparedImages,
    write_prepared_images,
)

_IMAGE_ENV_KEYS = (
    "CODEPROBE_AGENT_IMAGE",
    "CODEPROBE_SCORING_IMAGE",
    "CODEPROBE_IMAGE_REGISTRY",
    "CODEPROBE_IMAGE_NAMESPACE",
    "CODEPROBE_IMAGE_VERSION",
)
# CONTAINER_CONFIG_ENV is deliberately NOT cleared here. Deleting it makes
# load_prepared_images() fall back to ~/.codeprobe/container-images.json, so
# these tests read whatever the developer last bootstrapped — green in CI,
# red on a bootstrapped host (codeprobe-9yk6). The suite-wide
# _isolate_prepared_container_images fixture already points it at a path that
# does not exist; tests needing a prepared config setenv it themselves.
_AGENT_DIGEST = "sha256:" + "a" * 64
_SCORING_DIGEST = "sha256:" + "b" * 64
_AGENT_LOCAL_ID = "sha256:" + "c" * 64
_SCORING_LOCAL_ID = "sha256:" + "d" * 64


def _clear_image_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _IMAGE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _write_prepared_config(path: Path, *, engine: str = "docker") -> None:
    write_prepared_images(
        PreparedImages(
            engine=engine,  # type: ignore[arg-type]
            agent=PreparedImage(
                "registry.example/team/codeprobe-agent:0.13.0",
                f"registry.example/team/codeprobe-agent:0.13.0@{_AGENT_DIGEST}",
                _AGENT_DIGEST,
                _AGENT_LOCAL_ID,
            ),
            scoring=PreparedImage(
                "registry.example/team/codeprobe-scoring:0.13.0",
                f"registry.example/team/codeprobe-scoring:0.13.0@{_SCORING_DIGEST}",
                _SCORING_DIGEST,
                _SCORING_LOCAL_ID,
            ),
        ),
        path,
    )


@pytest.mark.parametrize(
    ("configured", "missing"),
    [
        ({}, "CODEPROBE_IMAGE_REGISTRY, CODEPROBE_IMAGE_NAMESPACE"),
        (
            {"CODEPROBE_IMAGE_REGISTRY": "registry.example.test"},
            "CODEPROBE_IMAGE_NAMESPACE",
        ),
        (
            {"CODEPROBE_IMAGE_NAMESPACE": "platform/codeprobe"},
            "CODEPROBE_IMAGE_REGISTRY",
        ),
    ],
)
def test_runtime_image_refs_report_exact_missing_registry_settings(
    monkeypatch: pytest.MonkeyPatch, configured: dict[str, str], missing: str
) -> None:
    _clear_image_env(monkeypatch)
    for key, value in configured.items():
        monkeypatch.setenv(key, value)

    expected = f"Missing required image setting(s): {missing}"
    with pytest.raises(ValueError) as agent_exc:
        sandbox_runner.agent_image_reference()
    assert expected in str(agent_exc.value)
    with pytest.raises(ValueError) as scoring_exc:
        sandbox_runner.scoring_image_reference()
    assert expected in str(scoring_exc.value)


def test_prepared_config_resolves_immutable_local_image_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_image_env(monkeypatch)
    path = tmp_path / "container-images.json"
    _write_prepared_config(path)
    monkeypatch.setenv(CONTAINER_CONFIG_ENV, str(path))

    assert sandbox_runner.agent_image_reference() == _AGENT_LOCAL_ID
    assert sandbox_runner.scoring_image_reference() == _SCORING_LOCAL_ID


def test_source_reference_helpers_ignore_prepared_runtime_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_image_env(monkeypatch)
    path = tmp_path / "container-images.json"
    _write_prepared_config(path)
    monkeypatch.setenv(CONTAINER_CONFIG_ENV, str(path))
    monkeypatch.setenv(
        sandbox_runner.AGENT_IMAGE_ENV,
        "private.example/team/agent:0.13.0",
    )
    monkeypatch.setenv(
        sandbox_runner.SCORING_IMAGE_ENV,
        "private.example/team/scoring:0.13.0",
    )

    assert (
        sandbox_runner.agent_source_image_reference()
        == "private.example/team/agent:0.13.0"
    )
    assert (
        sandbox_runner.scoring_source_image_reference()
        == "private.example/team/scoring:0.13.0"
    )


def test_changed_source_override_requires_rebootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_image_env(monkeypatch)
    path = tmp_path / "container-images.json"
    _write_prepared_config(path)
    monkeypatch.setenv(CONTAINER_CONFIG_ENV, str(path))
    monkeypatch.setenv(
        sandbox_runner.AGENT_IMAGE_ENV,
        "private.example/team/other-agent:0.13.0",
    )

    with pytest.raises(ValueError, match="re-run codeprobe bootstrap"):
        sandbox_runner.agent_image_reference()


def test_prepared_engine_wins_when_both_engines_are_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_image_env(monkeypatch)
    path = tmp_path / "container-images.json"
    _write_prepared_config(path, engine="podman")
    monkeypatch.setenv(CONTAINER_CONFIG_ENV, str(path))
    monkeypatch.setattr(
        sandbox_runner.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in ("docker", "podman") else None,
    )
    monkeypatch.setattr(
        sandbox_runner, "detect_engine", sandbox_runner._detect_engine
    )

    assert sandbox_runner.detect_engine() == "/usr/bin/podman"


def test_prepared_local_ids_are_valid_runtime_references(tmp_path: Path) -> None:
    agent_argv = containerize_argv(
        ["claude", "-p", "prompt"],
        engine="docker",
        workspace=tmp_path,
        config_dir=None,
        mcp_tmpfile=None,
        env_keys=[],
        image=_AGENT_LOCAL_ID,
        name="codeprobe-agent-test",
        env={},
    )
    scoring_argv = sandbox_runner._build_run_command(
        "docker",
        ["bash", "tests/test.sh"],
        {str(tmp_path): str(tmp_path)},
        allow_writes=True,
        image=_SCORING_LOCAL_ID,
        workdir=str(tmp_path),
        env=None,
    )

    assert _AGENT_LOCAL_ID in agent_argv
    assert _SCORING_LOCAL_ID in scoring_argv


def test_local_image_build_tags_track_installed_package_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_image_env(monkeypatch)
    release_version = package_version("codeprobe")

    assert sandbox_runner.agent_image_build_tag() == f"codeprobe-agent:{release_version}"
    assert (
        sandbox_runner.scoring_image_build_tag()
        == f"codeprobe-scoring:{release_version}"
    )


def test_image_reference_composes_registry_namespace_and_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_image_env(monkeypatch)
    monkeypatch.setenv("CODEPROBE_IMAGE_REGISTRY", "registry.example.test")
    monkeypatch.setenv("CODEPROBE_IMAGE_NAMESPACE", "platform/codeprobe")
    monkeypatch.setenv("CODEPROBE_IMAGE_VERSION", "1.2.3")

    assert (
        sandbox_runner.agent_image_reference()
        == "registry.example.test/platform/codeprobe/codeprobe-agent:1.2.3"
    )
    assert (
        sandbox_runner.scoring_image_reference()
        == "registry.example.test/platform/codeprobe/codeprobe-scoring:1.2.3"
    )


def test_exact_agent_and_scoring_image_overrides_win(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_image_env(monkeypatch)
    monkeypatch.setenv("CODEPROBE_IMAGE_REGISTRY", "registry.example.test")
    agent_digest = "a" * 64
    scoring_digest = "b" * 64
    monkeypatch.setenv(
        "CODEPROBE_AGENT_IMAGE", f"mirror.example/agent@sha256:{agent_digest}"
    )
    monkeypatch.setenv(
        "CODEPROBE_SCORING_IMAGE",
        f"mirror.example/scoring@sha256:{scoring_digest}",
    )

    assert (
        sandbox_runner.agent_image_reference()
        == f"mirror.example/agent@sha256:{agent_digest}"
    )
    assert (
        sandbox_runner.scoring_image_reference()
        == f"mirror.example/scoring@sha256:{scoring_digest}"
    )


def test_exact_image_override_accepts_ipv6_tag_and_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_image_env(monkeypatch)
    digest = "a" * 64
    reference = f"[2001:db8::1]:5000/platform/codeprobe-agent:1.2.3@sha256:{digest}"
    monkeypatch.setenv("CODEPROBE_AGENT_IMAGE", reference)

    assert sandbox_runner.agent_image_reference() == reference


@pytest.mark.parametrize(
    "reference",
    [
        "ubuntu:22.04",
        "acme/ns/codeprobe-agent:1.2.3",
    ],
)
def test_exact_image_override_requires_fully_qualified_registry(
    monkeypatch: pytest.MonkeyPatch, reference: str
) -> None:
    _clear_image_env(monkeypatch)
    monkeypatch.setenv("CODEPROBE_AGENT_IMAGE", reference)

    with pytest.raises(ValueError, match="fully qualified"):
        sandbox_runner.agent_image_reference()


def test_empty_image_override_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_image_env(monkeypatch)
    monkeypatch.setenv("CODEPROBE_AGENT_IMAGE", " ")

    with pytest.raises(ValueError, match="CODEPROBE_AGENT_IMAGE"):
        sandbox_runner.agent_image_reference()


@pytest.mark.parametrize(
    ("env_key", "value", "message"),
    [
        ("CODEPROBE_AGENT_IMAGE", "mirror.example/agent", "explicit tag or digest"),
        ("CODEPROBE_AGENT_IMAGE", "mirror.example/agent:latest", "latest"),
        ("CODEPROBE_AGENT_IMAGE", "https://mirror.example/agent:1.2.3", "not a URL"),
        ("CODEPROBE_AGENT_IMAGE", "mirror.example/agent@sha256:abc123", "sha256"),
        ("CODEPROBE_AGENT_IMAGE", "registry..example/team/agent:1.2.3", "invalid"),
        ("CODEPROBE_AGENT_IMAGE", "registry.-example/team/agent:1.2.3", "invalid"),
        ("CODEPROBE_AGENT_IMAGE", " mirror.example/agent:1.2.3", "whitespace"),
        ("CODEPROBE_AGENT_IMAGE", "mirror.example/agent:1.2.3 ", "whitespace"),
        ("CODEPROBE_IMAGE_REGISTRY", "REGISTRY.example.test", "registry host"),
        ("CODEPROBE_IMAGE_REGISTRY", "registry.example.test/", "registry host"),
        ("CODEPROBE_IMAGE_NAMESPACE", "/platform/codeprobe", "repository path"),
        ("CODEPROBE_IMAGE_NAMESPACE", "platform/codeprobe/", "repository path"),
        ("CODEPROBE_IMAGE_NAMESPACE", "platform//codeprobe", "repository path"),
        ("CODEPROBE_IMAGE_VERSION", "latest", "latest"),
    ],
)
def test_invalid_image_reference_parts_fail_fast(
    monkeypatch: pytest.MonkeyPatch,
    env_key: str,
    value: str,
    message: str,
) -> None:
    _clear_image_env(monkeypatch)
    monkeypatch.setenv(env_key, value)
    if env_key == "CODEPROBE_IMAGE_REGISTRY":
        monkeypatch.setenv("CODEPROBE_IMAGE_NAMESPACE", "platform/codeprobe")
    if env_key == "CODEPROBE_IMAGE_NAMESPACE":
        monkeypatch.setenv("CODEPROBE_IMAGE_REGISTRY", "registry.example.test")
    if env_key == "CODEPROBE_IMAGE_VERSION":
        monkeypatch.setenv("CODEPROBE_IMAGE_REGISTRY", "registry.example.test")
        monkeypatch.setenv("CODEPROBE_IMAGE_NAMESPACE", "platform/codeprobe")

    with pytest.raises(ValueError, match=message):
        sandbox_runner.agent_image_reference()


@pytest.mark.parametrize(
    ("registry", "expected"),
    [
        ("localhost", "localhost/platform/codeprobe/codeprobe-agent:1.2.3"),
        (
            "registry.example:5000",
            "registry.example:5000/platform/codeprobe/codeprobe-agent:1.2.3",
        ),
        (
            "[2001:db8::1]:5000",
            "[2001:db8::1]:5000/platform/codeprobe/codeprobe-agent:1.2.3",
        ),
    ],
)
def test_composed_image_reference_accepts_qualified_registry_hosts(
    monkeypatch: pytest.MonkeyPatch, registry: str, expected: str
) -> None:
    _clear_image_env(monkeypatch)
    monkeypatch.setenv("CODEPROBE_IMAGE_REGISTRY", registry)
    monkeypatch.setenv("CODEPROBE_IMAGE_NAMESPACE", "platform/codeprobe")
    monkeypatch.setenv("CODEPROBE_IMAGE_VERSION", "1.2.3")

    assert sandbox_runner.agent_image_reference() == expected


def test_composed_image_reference_rejects_single_label_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_image_env(monkeypatch)
    monkeypatch.setenv("CODEPROBE_IMAGE_REGISTRY", "acme")
    monkeypatch.setenv("CODEPROBE_IMAGE_NAMESPACE", "platform/codeprobe")
    monkeypatch.setenv("CODEPROBE_IMAGE_VERSION", "1.2.3")

    with pytest.raises(ValueError, match="registry host"):
        sandbox_runner.agent_image_reference()
