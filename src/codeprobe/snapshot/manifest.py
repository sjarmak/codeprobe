"""Extended SNAPSHOT.json manifest builder for R18 CSB-layout snapshots.

R14 produces a base manifest with ``mode``, ``source``, ``files``,
``attestation`` (and optional ``canary_result``). R18 extends it with:

- ``schema_version`` — manifest schema version.
- ``created_at`` — ISO-8601 UTC timestamp.
- ``dependencies`` — a dependency-surface block recording MCP tool schemas,
  LLM model IDs per backend, issue-tracker API versions, and build-manifest
  parser versions present at mine/run time.

The extended block sits alongside (not inside) the r14 attestation body, so
the attestation signature computed by r14 is unaffected. R18 layers its own
file-hash re-verification pass on top of r14's body attestation to catch
tampering of file bodies.

No LLM is invoked from this module.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from codeprobe.snapshot.redact import SnapshotManifest

__all__ = [
    "Dependencies",
    "ExtendedManifest",
    "SNAPSHOT_SCHEMA_VERSION",
    "build_extended_manifest",
    "collect_dependencies",
    "manifest_to_json_dict",
    "write_extended_manifest",
]


SNAPSHOT_SCHEMA_VERSION: str = "1.0"


# ---------------------------------------------------------------------------
# Dependency-surface dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Dependencies:
    """Snapshot of the dependency surface at mine/run time.

    Each field is serialisable via :func:`dataclasses.asdict`. Lists of dicts
    are used rather than typed dataclasses so the shape stays forward-compatible
    — readers should treat unknown keys as opaque.
    """

    mcp_tools: list[dict[str, Any]] = field(default_factory=list)
    llm_backends: list[dict[str, Any]] = field(default_factory=list)
    issue_trackers: list[dict[str, Any]] = field(default_factory=list)
    build_manifest_parsers: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class ExtendedManifest:
    """R18 manifest = r14 manifest + schema_version + created_at + dependencies."""

    base: SnapshotManifest
    schema_version: str
    created_at: str
    dependencies: Dependencies


# ---------------------------------------------------------------------------
# Dependency collection
# ---------------------------------------------------------------------------


def _collect_mcp_tools() -> list[dict[str, Any]]:
    """Return the MCP capability surface.

    Each entry records the capability id, name, the overall capabilities
    registry version, and a stable hash of the input schema so downstream
    consumers can detect schema drift without having to deep-compare.
    """
    try:
        from codeprobe.mcp.capabilities import CAPABILITIES_VERSION, list_capabilities
    except Exception:
        return []

    tools: list[dict[str, Any]] = []
    for cap in list_capabilities():
        schema_bytes = json.dumps(
            dict(cap.input_schema), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        tools.append(
            {
                "id": cap.id,
                "name": cap.name,
                "capabilities_version": CAPABILITIES_VERSION,
                "input_schema_sha256": hashlib.sha256(schema_bytes).hexdigest(),
            }
        )
    return tools


def _collect_llm_backends() -> list[dict[str, Any]]:
    """Return per-logical-name backend identifiers from the r13 registry."""
    try:
        from codeprobe.llm import get_registry
    except Exception:
        return []

    try:
        registry = get_registry()
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for logical in registry.logical_names():
        per_backend: dict[str, Any] = {}
        for backend in registry.backends_for(logical):
            try:
                per_backend[backend] = registry.resolve(logical, backend)
            except Exception:
                # Skip a single failing resolution rather than dropping the row.
                continue
        out.append({"logical_name": logical, "per_backend_ids": per_backend})
    return out


def _collect_issue_trackers() -> list[dict[str, Any]]:
    """Return the issue-tracker API versions codeprobe integrates with.

    The versions are derived structurally from the URL templates baked into
    the corresponding adapters — e.g. ``jira.py`` targets ``/rest/api/3/``
    and ``gitlab.py`` targets ``/api/v4/``. If an adapter later moves to a
    new major version, that adapter's URL template bumps and this list bumps
    with it.
    """
    return [
        {"name": "jira", "api_version": "v3"},
        {"name": "github", "api_version": "v3"},
        {"name": "gitlab", "api_version": "v4"},
    ]


def _collect_build_manifest_parsers() -> list[dict[str, Any]]:
    """Return the build-manifest parser versions used at mine/run time.

    codeprobe itself is the only build-manifest parser in this codebase; the
    version surfaced here is the installed codeprobe package version.
    """
    try:
        from codeprobe import __version__ as codeprobe_version
    except Exception:
        codeprobe_version = "unknown"
    return [{"name": "codeprobe", "version": codeprobe_version}]


def collect_dependencies() -> Dependencies:
    """Gather the full dependency surface in one call."""
    return Dependencies(
        mcp_tools=_collect_mcp_tools(),
        llm_backends=_collect_llm_backends(),
        issue_trackers=_collect_issue_trackers(),
        build_manifest_parsers=_collect_build_manifest_parsers(),
    )


# ---------------------------------------------------------------------------
# Manifest construction and serialisation
# ---------------------------------------------------------------------------


def build_extended_manifest(
    base: SnapshotManifest,
    dependencies: Dependencies | None = None,
    created_at: str | None = None,
) -> ExtendedManifest:
    """Produce an :class:`ExtendedManifest` from the r14 base manifest."""
    deps = dependencies if dependencies is not None else collect_dependencies()
    timestamp = created_at if created_at is not None else datetime.now(
        UTC
    ).isoformat()
    return ExtendedManifest(
        base=base,
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        created_at=timestamp,
        dependencies=deps,
    )


def manifest_to_json_dict(ext: ExtendedManifest) -> dict[str, Any]:
    """Serialise an :class:`ExtendedManifest` to the on-disk JSON shape.

    The r14 top-level keys (``mode``, ``source``, ``files``,
    ``attestation``, optional ``canary_result``) are preserved unchanged.
    The R18 keys (``schema_version``, ``created_at``, ``dependencies``) are
    added at the top level, alongside (not inside) the r14 payload.
    """
    body: dict[str, Any] = ext.base.to_dict()
    body["schema_version"] = ext.schema_version
    body["created_at"] = ext.created_at
    body["dependencies"] = asdict(ext.dependencies)
    return body


def write_extended_manifest(ext: ExtendedManifest, snapshot_dir: Path) -> Path:
    """Write ``SNAPSHOT.json`` to ``snapshot_dir`` and return the path."""
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    target = snapshot_dir / "SNAPSHOT.json"
    target.write_text(
        json.dumps(
            manifest_to_json_dict(ext),
            sort_keys=True,
            indent=2,
            separators=(",", ": "),
        )
    )
    return target
