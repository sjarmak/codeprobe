"""Tests for provider-neutral runtime OCI image references."""

from __future__ import annotations

from importlib.metadata import version as package_version

import pytest

from codeprobe.sandbox import runner as sandbox_runner

_IMAGE_ENV_KEYS = (
    "CODEPROBE_AGENT_IMAGE",
    "CODEPROBE_SCORING_IMAGE",
    "CODEPROBE_IMAGE_REGISTRY",
    "CODEPROBE_IMAGE_NAMESPACE",
    "CODEPROBE_IMAGE_VERSION",
)


def _clear_image_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _IMAGE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_runtime_image_refs_require_registry_and_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_image_env(monkeypatch)

    with pytest.raises(ValueError, match="Set both CODEPROBE_IMAGE_REGISTRY"):
        sandbox_runner.agent_image_reference()
    with pytest.raises(ValueError, match="Set both CODEPROBE_IMAGE_REGISTRY"):
        sandbox_runner.scoring_image_reference()


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
