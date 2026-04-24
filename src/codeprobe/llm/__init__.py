"""Logical LLM model registry and multi-backend adapter matrix.

The registry maps stable logical names (``opus-4.7``, ``sonnet-4.6``,
``haiku-4.5``) to per-backend identifiers for Anthropic's public API,
Amazon Bedrock, Google Vertex AI, Azure OpenAI, and generic
OpenAI-compatible gateways.

Swapping a value in ``model_registry.yaml`` (e.g. pointing a Bedrock
entry at a new inference-profile ARN) is picked up without any code
change — callers always ask the registry at runtime.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml

__all__ = [
    "ModelRegistry",
    "RegistryError",
    "RegistryLoadError",
    "UnknownBackendError",
    "UnknownLogicalNameError",
    "DEFAULT_REGISTRY_PATH",
    "get_registry",
    "load_registry",
]


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RegistryError(Exception):
    """Base error for registry operations."""


class RegistryLoadError(RegistryError):
    """Raised when ``model_registry.yaml`` cannot be parsed."""


class UnknownLogicalNameError(RegistryError):
    """Raised when a logical model name is not defined in the registry."""


class UnknownBackendError(RegistryError):
    """Raised when a backend is not defined for a logical model."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


DEFAULT_REGISTRY_PATH: Path = Path(__file__).parent / "model_registry.yaml"


@dataclass(frozen=True)
class _RegistryData:
    """Immutable snapshot of a parsed registry file."""

    path: Path
    models: dict[str, dict[str, Any]]


class ModelRegistry:
    """Lookup table mapping logical model names to per-backend identifiers.

    The registry is re-readable: call :meth:`reload` to pick up changes on
    disk without restarting the process.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._data: _RegistryData = _load(path or DEFAULT_REGISTRY_PATH)

    @property
    def path(self) -> Path:
        return self._data.path

    def reload(self, path: Path | None = None) -> None:
        """Re-read the registry YAML (default path or the one supplied)."""
        self._data = _load(path or self._data.path)

    def logical_names(self) -> list[str]:
        return sorted(self._data.models.keys())

    def backends_for(self, logical_name: str) -> list[str]:
        entry = self._require_logical(logical_name)
        return sorted(entry.keys())

    def resolve(self, logical_name: str, backend: str) -> str | dict[str, Any]:
        """Return the backend-specific identifier for ``logical_name``.

        Returns a string for scalar entries (anthropic, bedrock, vertex,
        openai_compat) and a dict for structured entries (azure_openai).
        """
        entry = self._require_logical(logical_name)
        if backend not in entry:
            known = ", ".join(sorted(entry.keys()))
            raise UnknownBackendError(
                f"Backend {backend!r} not defined for logical model "
                f"{logical_name!r}. Known backends: {known}"
            )
        return cast("str | dict[str, Any]", entry[backend])

    def _require_logical(self, logical_name: str) -> dict[str, Any]:
        entry = self._data.models.get(logical_name)
        if entry is None:
            known = ", ".join(sorted(self._data.models.keys()))
            raise UnknownLogicalNameError(
                f"Logical model {logical_name!r} not defined in registry at "
                f"{self._data.path}. Known names: {known}"
            )
        return entry


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def _load(path: Path) -> _RegistryData:
    if not path.exists():
        raise RegistryLoadError(f"Registry file not found: {path}")
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise RegistryLoadError(f"Malformed YAML in {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise RegistryLoadError(
            f"Registry top-level must be a mapping, got {type(raw).__name__} in {path}"
        )

    models = raw.get("models")
    if not isinstance(models, dict) or not models:
        raise RegistryLoadError(
            f"Registry must define a non-empty 'models' mapping in {path}"
        )

    normalized: dict[str, dict[str, Any]] = {}
    for logical, entry in models.items():
        if not isinstance(entry, dict) or not entry:
            raise RegistryLoadError(
                f"Model {logical!r} must map to a non-empty dict of backends"
            )
        normalized[str(logical)] = dict(entry)

    return _RegistryData(path=path, models=normalized)


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


_DEFAULT: ModelRegistry | None = None
_DEFAULT_LOCK = threading.Lock()


def get_registry() -> ModelRegistry:
    """Return a process-wide default registry (lazily initialised).

    Thread-safe via double-checked locking so concurrent callers see the
    same instance without paying for a lock on every access after init.
    """
    global _DEFAULT
    if _DEFAULT is None:
        with _DEFAULT_LOCK:
            if _DEFAULT is None:
                _DEFAULT = ModelRegistry()
    return _DEFAULT


def load_registry(path: Path) -> ModelRegistry:
    """Load a registry from ``path`` (does not affect the process default)."""
    return ModelRegistry(path=path)
