"""Verifies that swapping an identifier in model_registry.yaml is picked
up without code changes — satisfies acceptance criterion 5."""

from __future__ import annotations

from pathlib import Path

import pytest

from codeprobe.llm import (
    ModelRegistry,
    RegistryLoadError,
    load_registry,
)

BASELINE_YAML = """\
models:
  opus-4.7:
    anthropic: claude-opus-4-7
    bedrock: arn:aws:bedrock:us-east-1:000000000000:inference-profile/old-profile
    vertex: publishers/anthropic/models/claude-opus-4-7
    azure_openai:
      deployment: opus-old
      api_version: '2024-10-21'
    openai_compat: claude-opus-4-7
"""

SWAPPED_YAML = """\
models:
  opus-4.7:
    anthropic: claude-opus-4-7
    bedrock: arn:aws:bedrock:us-west-2:999999999999:inference-profile/brand-new-profile
    vertex: publishers/anthropic/models/claude-opus-4-7
    azure_openai:
      deployment: opus-new
      api_version: '2024-12-01'
    openai_compat: claude-opus-4-7
"""


def test_swapped_bedrock_arn_is_picked_up_on_reload(tmp_path: Path) -> None:
    registry_path = tmp_path / "model_registry.yaml"
    registry_path.write_text(BASELINE_YAML, encoding="utf-8")

    registry = load_registry(registry_path)
    original = registry.resolve("opus-4.7", "bedrock")
    assert original.endswith("old-profile")

    # Swap the ARN on disk — no code change.
    registry_path.write_text(SWAPPED_YAML, encoding="utf-8")
    registry.reload()

    updated = registry.resolve("opus-4.7", "bedrock")
    assert updated.endswith("brand-new-profile")
    assert updated != original
    assert "us-west-2" in updated

    azure = registry.resolve("opus-4.7", "azure_openai")
    assert isinstance(azure, dict)
    assert azure["deployment"] == "opus-new"
    assert azure["api_version"] == "2024-12-01"


def test_reload_with_explicit_path(tmp_path: Path) -> None:
    first = tmp_path / "first.yaml"
    second = tmp_path / "second.yaml"
    first.write_text(BASELINE_YAML, encoding="utf-8")
    second.write_text(SWAPPED_YAML, encoding="utf-8")

    registry = ModelRegistry(path=first)
    assert registry.resolve("opus-4.7", "bedrock").endswith("old-profile")

    registry.reload(path=second)
    assert registry.resolve("opus-4.7", "bedrock").endswith("brand-new-profile")
    assert registry.path == second


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(RegistryLoadError):
        ModelRegistry(path=tmp_path / "does-not-exist.yaml")


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("models: [this is not a mapping", encoding="utf-8")
    with pytest.raises(RegistryLoadError):
        ModelRegistry(path=bad)


def test_empty_models_section_raises(tmp_path: Path) -> None:
    empty = tmp_path / "empty.yaml"
    empty.write_text("models: {}\n", encoding="utf-8")
    with pytest.raises(RegistryLoadError):
        ModelRegistry(path=empty)
