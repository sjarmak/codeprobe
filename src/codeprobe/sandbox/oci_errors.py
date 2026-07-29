"""Parse the narrow registry-absence contract emitted by ORAS."""

from __future__ import annotations

from typing import Final

_LEGACY_ABSENCE_MESSAGES: Final[frozenset[str]] = frozenset(
    {"manifest unknown", "name unknown"}
)
_ORAS_REGISTRY_PREFIX: Final[str] = (
    "Error response from registry: failed to resolve digest: "
)


def is_exact_registry_absence(stderr: str, reference: str) -> bool:
    """Return whether *stderr* is an exact, known terminal not-found response."""
    normalized = stderr.strip()
    if normalized.lower() in _LEGACY_ABSENCE_MESSAGES:
        return True
    return normalized == f"{_ORAS_REGISTRY_PREFIX}{reference}: not found"
