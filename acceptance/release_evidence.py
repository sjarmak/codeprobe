"""Tracked handoff for acceptance verdicts used by release CI."""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
VERDICT_NAMES = ("verdict-previous.json", "verdict-latest.json")


class ReleaseEvidenceError(ValueError):
    """Release evidence is missing, stale, malformed, or altered."""


def export_release_evidence(
    verdict_paths: list[Path],
    evidence_dir: Path,
    release_version: str,
) -> Path:
    """Copy exactly two verdicts into a tracked, version-bound evidence set."""
    if len(verdict_paths) != len(VERDICT_NAMES):
        raise ReleaseEvidenceError("exactly two acceptance verdicts are required")
    if not release_version:
        raise ReleaseEvidenceError("release version must not be empty")

    source_contents = [_read_bytes(path) for path in verdict_paths]
    evidence_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, str]] = []
    for name, content in zip(VERDICT_NAMES, source_contents, strict=True):
        destination = evidence_dir / name
        destination.write_bytes(content)
        records.append(
            {
                "path": name,
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )

    manifest_path = evidence_dir / MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "release_version": release_version,
                "verdicts": records,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest_path


def load_release_evidence(
    evidence_dir: Path,
    expected_version: str,
) -> list[Path]:
    """Validate the tracked manifest and return its two ordered verdict paths."""
    manifest_path = evidence_dir / MANIFEST_NAME
    manifest = _read_manifest(manifest_path)

    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ReleaseEvidenceError(
            f"{manifest_path} must use schema_version={SCHEMA_VERSION}"
        )
    if manifest.get("release_version") != expected_version:
        raise ReleaseEvidenceError(
            f"{manifest_path} is not bound to release version {expected_version}"
        )

    records = manifest.get("verdicts")
    if not isinstance(records, list) or len(records) != len(VERDICT_NAMES):
        raise ReleaseEvidenceError(
            f"{manifest_path} must reference exactly two acceptance verdicts"
        )

    verdict_paths: list[Path] = []
    for expected_name, record in zip(VERDICT_NAMES, records, strict=True):
        if not isinstance(record, dict) or record.get("path") != expected_name:
            raise ReleaseEvidenceError(
                f"{manifest_path} must reference {expected_name} in order"
            )
        expected_digest = record.get("sha256")
        if not isinstance(expected_digest, str):
            raise ReleaseEvidenceError(
                f"{manifest_path} has no SHA-256 digest for {expected_name}"
            )
        verdict_path = evidence_dir / expected_name
        actual_digest = hashlib.sha256(_read_bytes(verdict_path)).hexdigest()
        if not hmac.compare_digest(actual_digest, expected_digest):
            raise ReleaseEvidenceError(
                f"{verdict_path} does not match its manifest SHA-256 digest"
            )
        verdict_paths.append(verdict_path)
    return verdict_paths


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ReleaseEvidenceError(f"cannot read release evidence {path}: {exc}") from exc


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseEvidenceError(f"cannot load release manifest {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ReleaseEvidenceError(f"release manifest {path} must be a JSON object")
    return loaded
