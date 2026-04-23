"""Tests for the logical -> backend model registry."""

from __future__ import annotations

import pytest

from codeprobe.llm import (
    DEFAULT_REGISTRY_PATH,
    ModelRegistry,
    UnknownBackendError,
    UnknownLogicalNameError,
    get_registry,
)
from codeprobe.llm.backends import (
    AnthropicBackend,
    AzureOpenAIBackend,
    BedrockBackend,
    OpenAICompatBackend,
    VertexBackend,
    get_backend,
)

EXPECTED_LOGICAL = {"opus-4.7", "sonnet-4.6", "haiku-4.5"}
EXPECTED_BACKENDS = {
    "anthropic",
    "bedrock",
    "vertex",
    "azure_openai",
    "openai_compat",
}


def test_default_registry_file_exists() -> None:
    assert DEFAULT_REGISTRY_PATH.exists(), (
        f"model_registry.yaml must ship inside the package: {DEFAULT_REGISTRY_PATH}"
    )


def test_default_registry_has_all_logical_names() -> None:
    registry = get_registry()
    assert set(registry.logical_names()) == EXPECTED_LOGICAL


def test_default_registry_has_all_backends_per_logical() -> None:
    registry = get_registry()
    for logical in EXPECTED_LOGICAL:
        assert set(registry.backends_for(logical)) == EXPECTED_BACKENDS, (
            f"Incomplete backend coverage for {logical}"
        )


def test_resolve_opus_on_anthropic_is_valid_slug() -> None:
    """Acceptance criterion 3: resolving 'opus-4.7' on 'anthropic' gives a valid slug."""
    registry = get_registry()
    value = registry.resolve("opus-4.7", "anthropic")
    assert isinstance(value, str)
    assert value, "Resolved model slug must not be empty"
    assert value.startswith("claude-opus-4-7"), (
        f"Expected Anthropic opus slug to start with claude-opus-4-7, got {value!r}"
    )


def test_resolve_azure_returns_dict_with_deployment_and_api_version() -> None:
    registry = get_registry()
    value = registry.resolve("sonnet-4.6", "azure_openai")
    assert isinstance(value, dict)
    assert value["deployment"]
    assert value["api_version"]


def test_resolve_bedrock_returns_arn() -> None:
    registry = get_registry()
    value = registry.resolve("haiku-4.5", "bedrock")
    assert isinstance(value, str)
    assert value.startswith("arn:aws:bedrock:"), (
        f"Expected Bedrock ARN, got {value!r}"
    )


def test_unknown_logical_name_raises() -> None:
    registry = get_registry()
    with pytest.raises(UnknownLogicalNameError):
        registry.resolve("gpt-9", "anthropic")


def test_unknown_backend_raises() -> None:
    registry = get_registry()
    with pytest.raises(UnknownBackendError):
        registry.resolve("opus-4.7", "palm")


def test_full_matrix_resolves() -> None:
    registry = get_registry()
    for logical in EXPECTED_LOGICAL:
        for backend in EXPECTED_BACKENDS:
            value = registry.resolve(logical, backend)
            assert value, f"{logical}/{backend} resolved to empty value"


@pytest.mark.parametrize(
    "backend_cls,expected_name",
    [
        (AnthropicBackend, "anthropic"),
        (BedrockBackend, "bedrock"),
        (VertexBackend, "vertex"),
        (AzureOpenAIBackend, "azure_openai"),
        (OpenAICompatBackend, "openai_compat"),
    ],
)
def test_backend_resolve_model_id_delegates_to_registry(
    backend_cls: type, expected_name: str
) -> None:
    backend = backend_cls()
    assert backend.name == expected_name
    value = backend.resolve_model_id("opus-4.7")
    assert value  # non-empty (str or dict)


def test_get_backend_factory_returns_instances_of_correct_class() -> None:
    assert isinstance(get_backend("anthropic"), AnthropicBackend)
    assert isinstance(get_backend("bedrock"), BedrockBackend)
    assert isinstance(get_backend("vertex"), VertexBackend)
    assert isinstance(get_backend("azure_openai"), AzureOpenAIBackend)
    assert isinstance(get_backend("openai_compat"), OpenAICompatBackend)


def test_explicit_registry_path_loads() -> None:
    registry = ModelRegistry(path=DEFAULT_REGISTRY_PATH)
    assert "opus-4.7" in registry.logical_names()
