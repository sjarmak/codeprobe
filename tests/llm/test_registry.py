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
from codeprobe.llm.backends.base import BackendExecutionError

EXPECTED_LOGICAL_VERSIONED = {"opus-4.7", "sonnet-4.6", "haiku-4.5"}
# Short aliases forward-compatible with codeprobe.core.llm callers.
EXPECTED_LOGICAL_ALIASES = {"opus", "sonnet", "haiku"}
EXPECTED_LOGICAL = EXPECTED_LOGICAL_VERSIONED | EXPECTED_LOGICAL_ALIASES
EXPECTED_BACKENDS = {
    "anthropic",
    "bedrock",
    "vertex",
    "azure_openai",
    "openai_compat",
}

# Short alias -> canonical versioned name. Used to assert parity between
# the two forms so there is a single source of truth for each logical
# model.
ALIAS_TO_VERSIONED: dict[str, str] = {
    "opus": "opus-4.7",
    "sonnet": "sonnet-4.6",
    "haiku": "haiku-4.5",
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


def test_bedrock_placeholder_arn_raises_prescriptive_error() -> None:
    """The shipped registry carries a placeholder AWS account ID so that
    an un-customized checkout fails loudly instead of hitting AWS with
    a bogus ARN. ``BedrockBackend.resolve_model_id`` must reject it.
    """
    backend = BedrockBackend()
    with pytest.raises(BackendExecutionError) as exc_info:
        backend.resolve_model_id("opus-4.7")
    message = str(exc_info.value)
    assert "placeholder account ID" in message
    assert "REPLACE_WITH_YOUR_AWS_ACCOUNT_ID" in message


def test_bedrock_legacy_zero_placeholder_also_raises(tmp_path) -> None:
    """Defence-in-depth: even if a user hand-rolls an ARN with the
    legacy ``:000000000000:`` stub, the backend must still refuse."""
    from codeprobe.llm import load_registry

    yaml_text = (
        "models:\n"
        "  opus-4.7:\n"
        "    anthropic: claude-opus-4-7\n"
        "    bedrock: arn:aws:bedrock:us-east-1:000000000000:inference-profile/x\n"
        "    vertex: publishers/anthropic/models/claude-opus-4-7\n"
        "    azure_openai:\n"
        "      deployment: d\n"
        "      api_version: '2024-10-21'\n"
        "    openai_compat: claude-opus-4-7\n"
    )
    path = tmp_path / "registry.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    custom = load_registry(path)
    backend = BedrockBackend(registry=custom)
    with pytest.raises(BackendExecutionError, match="placeholder account ID"):
        backend.resolve_model_id("opus-4.7")


def test_bedrock_valid_arn_resolves_cleanly(tmp_path) -> None:
    """A fully customized ARN must round-trip without raising."""
    from codeprobe.llm import load_registry

    yaml_text = (
        "models:\n"
        "  opus-4.7:\n"
        "    anthropic: claude-opus-4-7\n"
        "    bedrock: arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.x\n"
        "    vertex: publishers/anthropic/models/claude-opus-4-7\n"
        "    azure_openai:\n"
        "      deployment: d\n"
        "      api_version: '2024-10-21'\n"
        "    openai_compat: claude-opus-4-7\n"
    )
    path = tmp_path / "registry.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    custom = load_registry(path)
    backend = BedrockBackend(registry=custom)
    value = backend.resolve_model_id("opus-4.7")
    assert value.endswith(":inference-profile/us.x")
    assert "123456789012" in value


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
        # BedrockBackend excluded here because the shipped registry
        # carries a placeholder AWS account ID that resolve_model_id is
        # contractually required to reject. See
        # ``test_bedrock_placeholder_arn_raises_prescriptive_error`` and
        # ``test_bedrock_valid_arn_resolves_cleanly`` for the pair.
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


@pytest.mark.parametrize("alias,versioned", sorted(ALIAS_TO_VERSIONED.items()))
def test_short_alias_resolves_to_same_anthropic_slug_as_versioned(
    alias: str, versioned: str
) -> None:
    """Single-source-of-truth invariant: the short logical names used
    by ``codeprobe.core.llm`` (``opus``, ``sonnet``, ``haiku``) must
    resolve to the same Anthropic slug as their versioned counterparts.
    """
    registry = get_registry()
    assert registry.resolve(alias, "anthropic") == registry.resolve(
        versioned, "anthropic"
    )


def test_registry_opus_alias_matches_core_llm_constant() -> None:
    """``core/llm.py``'s short-alias Anthropic model strings must agree
    with the registry so there is one source of truth for model IDs.
    """
    from codeprobe.core.llm import _ANTHROPIC_MODELS

    registry = get_registry()
    # ``_ANTHROPIC_MODELS`` may include versioned suffixes (e.g.
    # ``claude-haiku-4-5-20251001``); assert the base slug prefix matches
    # so the two stacks point at the same model family.
    for alias in ("opus", "sonnet", "haiku"):
        registry_slug = registry.resolve(alias, "anthropic")
        legacy_slug = _ANTHROPIC_MODELS[alias]
        assert isinstance(registry_slug, str)
        assert legacy_slug.startswith(registry_slug), (
            f"core.llm alias {alias!r} = {legacy_slug!r} does not start "
            f"with registry base slug {registry_slug!r}"
        )


def test_get_backend_factory_returns_instances_of_correct_class() -> None:
    assert isinstance(get_backend("anthropic"), AnthropicBackend)
    assert isinstance(get_backend("bedrock"), BedrockBackend)
    assert isinstance(get_backend("vertex"), VertexBackend)
    assert isinstance(get_backend("azure_openai"), AzureOpenAIBackend)
    assert isinstance(get_backend("openai_compat"), OpenAICompatBackend)


def test_explicit_registry_path_loads() -> None:
    registry = ModelRegistry(path=DEFAULT_REGISTRY_PATH)
    assert "opus-4.7" in registry.logical_names()


def test_get_registry_is_thread_safe() -> None:
    """Two threads racing to initialise the singleton must see the same
    instance — protects against double-construction under concurrency.
    """
    import threading

    import codeprobe.llm as llm_module

    # Reset the module-level singleton so both threads race the init path.
    with llm_module._DEFAULT_LOCK:
        llm_module._DEFAULT = None

    results: list[ModelRegistry] = []
    barrier = threading.Barrier(2)

    def worker() -> None:
        barrier.wait()
        results.append(llm_module.get_registry())

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    assert results[0] is results[1], (
        "get_registry() returned distinct instances under contention"
    )
