"""Parse, validate, and topologically sort acceptance criteria from TOML.

The manifest lives alongside this module at ``acceptance/criteria.toml`` and
encodes acceptance criteria derived from the project PRDs in ``docs/prd/``.

Parsing uses the Python 3.11+ stdlib ``tomllib`` so no third-party dependency
is required. Validation is strict — any missing required field, unknown tier
or severity, unresolved dependency, or dependency cycle raises ``ValueError``
so malformed manifests fail loudly at load time rather than at check time.

Downstream runners call :func:`load_criteria` once at startup, then iterate
the returned list in dependency order (topological sort is applied before the
list is returned).

See ``docs/prd/`` for the behavioural contracts these criteria encode and
the project CLAUDE.md for the ZFC principles they help enforce.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Allowed values for the discrete enum-like fields. Kept as module-level
# frozensets so callers can import and cross-reference them without copying
# the strings.
ALLOWED_TIERS: frozenset[str] = frozenset({"structural", "behavioral", "statistical"})
ALLOWED_SEVERITIES: frozenset[str] = frozenset({"critical", "high", "medium", "low"})
REQUIRED_FIELDS: tuple[str, ...] = (
    "id",
    "description",
    "tier",
    "check_type",
    "severity",
    "prd_source",
)

_DEFAULT_MANIFEST = Path(__file__).resolve().parent / "criteria.toml"


@dataclass(frozen=True)
class Criterion:
    """A single acceptance criterion loaded from the TOML manifest.

    Fields mirror the on-disk TOML schema. ``depends_on`` is normalised to a
    tuple so instances remain hashable, and ``params`` is stored as a plain
    dict because the parameter schema is check-type specific and validated
    at execution time by the check runner, not at load time.
    """

    id: str
    description: str
    tier: str
    check_type: str
    severity: str
    prd_source: str
    depends_on: tuple[str, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)


def load_criteria(path: Path | str | None = None) -> list[Criterion]:
    """Load, validate, and topologically sort criteria from the manifest.

    Args:
        path: Optional override for the manifest path. Defaults to the
            ``criteria.toml`` shipped alongside this module.

    Returns:
        A list of :class:`Criterion` instances in dependency order — every
        criterion appears after all of its ``depends_on`` targets.

    Raises:
        FileNotFoundError: The manifest file does not exist.
        ValueError: The manifest is malformed (missing required fields,
            unknown tier/severity, duplicate IDs, unresolved dependencies,
            or a dependency cycle).
    """
    manifest_path = Path(path) if path is not None else _DEFAULT_MANIFEST
    if not manifest_path.is_file():
        raise FileNotFoundError(f"Criteria manifest not found: {manifest_path}")

    with manifest_path.open("rb") as fh:
        data = tomllib.load(fh)

    raw_entries = data.get("criterion")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise ValueError(
            f"Manifest {manifest_path} must contain a non-empty " f"[[criterion]] array"
        )

    criteria = [_parse_entry(entry, index=i) for i, entry in enumerate(raw_entries)]
    _validate_unique_ids(criteria)
    _validate_dependencies(criteria)
    return topological_sort(criteria)


def filter_by_tier(criteria: Iterable[Criterion], tier: str) -> list[Criterion]:
    """Return criteria whose ``tier`` equals ``tier``.

    Args:
        criteria: Any iterable of criteria.
        tier: One of :data:`ALLOWED_TIERS`.

    Raises:
        ValueError: If ``tier`` is not a known tier.
    """
    if tier not in ALLOWED_TIERS:
        raise ValueError(
            f"Unknown tier {tier!r}; expected one of {sorted(ALLOWED_TIERS)}"
        )
    return [c for c in criteria if c.tier == tier]


def filter_by_severity(criteria: Iterable[Criterion], severity: str) -> list[Criterion]:
    """Return criteria whose ``severity`` equals ``severity``.

    Args:
        criteria: Any iterable of criteria.
        severity: One of :data:`ALLOWED_SEVERITIES`.

    Raises:
        ValueError: If ``severity`` is not a known severity.
    """
    if severity not in ALLOWED_SEVERITIES:
        raise ValueError(
            f"Unknown severity {severity!r}; expected one of "
            f"{sorted(ALLOWED_SEVERITIES)}"
        )
    return [c for c in criteria if c.severity == severity]


def topological_sort(criteria: list[Criterion]) -> list[Criterion]:
    """Return criteria in dependency order.

    Uses Kahn's algorithm. Within each "layer" (criteria with the same depth
    in the DAG) the original insertion order is preserved so the output is
    deterministic. Raises ``ValueError`` if a cycle is detected.
    """
    by_id: dict[str, Criterion] = {c.id: c for c in criteria}
    # indegree = number of unresolved dependencies
    indegree: dict[str, int] = {c.id: len(c.depends_on) for c in criteria}
    # reverse edges: dependency -> dependents
    dependents: dict[str, list[str]] = {c.id: [] for c in criteria}
    for c in criteria:
        for dep in c.depends_on:
            dependents[dep].append(c.id)

    # Seed the queue with zero-indegree nodes in insertion order.
    ready: list[str] = [c.id for c in criteria if indegree[c.id] == 0]
    sorted_ids: list[str] = []

    while ready:
        # Stable order: pop from the front, preserving insertion order.
        current = ready.pop(0)
        sorted_ids.append(current)
        for dep_id in dependents[current]:
            indegree[dep_id] -= 1
            if indegree[dep_id] == 0:
                ready.append(dep_id)

    if len(sorted_ids) != len(criteria):
        unresolved = [cid for cid, deg in indegree.items() if deg > 0]
        raise ValueError(
            f"Cycle detected in criteria dependency graph; "
            f"unresolved: {sorted(unresolved)}"
        )
    return [by_id[cid] for cid in sorted_ids]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_entry(entry: Any, index: int) -> Criterion:
    """Parse a single raw TOML table into a :class:`Criterion`.

    ``index`` is included in error messages so malformed entries can be
    located even when ``id`` is missing.
    """
    if not isinstance(entry, dict):
        raise ValueError(
            f"criterion[{index}] must be a table, got {type(entry).__name__}"
        )

    missing = [f for f in REQUIRED_FIELDS if not entry.get(f)]
    if missing:
        raise ValueError(
            f"criterion[{index}] (id={entry.get('id', '<missing>')}) "
            f"is missing required fields: {missing}"
        )

    tier = entry["tier"]
    if tier not in ALLOWED_TIERS:
        raise ValueError(
            f"criterion[{index}] id={entry['id']!r} has unknown tier "
            f"{tier!r}; expected one of {sorted(ALLOWED_TIERS)}"
        )

    severity = entry["severity"]
    if severity not in ALLOWED_SEVERITIES:
        raise ValueError(
            f"criterion[{index}] id={entry['id']!r} has unknown severity "
            f"{severity!r}; expected one of {sorted(ALLOWED_SEVERITIES)}"
        )

    depends_on_raw = entry.get("depends_on", [])
    if not isinstance(depends_on_raw, list) or not all(
        isinstance(d, str) for d in depends_on_raw
    ):
        raise ValueError(
            f"criterion[{index}] id={entry['id']!r} has invalid depends_on; "
            f"expected list[str]"
        )

    params_raw = entry.get("params", {})
    if not isinstance(params_raw, dict):
        raise ValueError(
            f"criterion[{index}] id={entry['id']!r} has invalid params; "
            f"expected table"
        )

    return Criterion(
        id=str(entry["id"]),
        description=str(entry["description"]),
        tier=str(tier),
        check_type=str(entry["check_type"]),
        severity=str(severity),
        prd_source=str(entry["prd_source"]),
        depends_on=tuple(depends_on_raw),
        params=dict(params_raw),
    )


def _validate_unique_ids(criteria: list[Criterion]) -> None:
    seen: set[str] = set()
    duplicates: list[str] = []
    for c in criteria:
        if c.id in seen:
            duplicates.append(c.id)
        seen.add(c.id)
    if duplicates:
        raise ValueError(f"Duplicate criterion ids: {sorted(set(duplicates))}")


def _validate_dependencies(criteria: list[Criterion]) -> None:
    known = {c.id for c in criteria}
    for c in criteria:
        missing = [dep for dep in c.depends_on if dep not in known]
        if missing:
            raise ValueError(
                f"criterion id={c.id!r} depends on unknown criteria: {missing}"
            )
        if c.id in c.depends_on:
            raise ValueError(f"criterion id={c.id!r} depends on itself")
