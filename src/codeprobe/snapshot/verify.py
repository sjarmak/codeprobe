"""R18 snapshot verify — extends r14 verification with three new guarantees.

1. **Symlink containment** — every symlink inside the snapshot resolves to a
   path that is still inside the snapshot directory. A link to ``../../etc``
   (or to any absolute path outside the snapshot root) causes verification
   to fail.
2. **Per-file hash recheck** — every file body referenced by the manifest
   is re-hashed on disk and compared against the manifest entry. Single-byte
   tampering of either the body or the manifest flips the hash and fails
   verification.
3. **Attestation recheck** — delegated to r14's :func:`verify_snapshot` so
   the existing HMAC / unsigned flows keep working unchanged.

No LLM is invoked — all checks are mechanical IO + sha256.
"""

from __future__ import annotations

import json
import posixpath
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from codeprobe.snapshot.fairness import (
    FairnessLeak,
    FairnessResult,
    check_fairness,
)
from codeprobe.snapshot.manifest import SNAPSHOT_SCHEMA_VERSION
from codeprobe.snapshot.redact import (
    _MAX_MANIFEST_BYTES,
    VerificationResult,
    _parse_snapshot_json,
    _verify_snapshot_data,
)
from codeprobe.snapshot.safe_io import (
    SymlinkEscapeError,
    TreeInventory,
    inventory_tree,
)

__all__ = [
    "ExtendedVerificationResult",
    "FairnessLeak",
    "FairnessResult",
    "check_fairness",
    "unsafe_legacy_snapshot_reason",
    "verify_snapshot_extended",
]


@dataclass(frozen=True)
class ExtendedVerificationResult:
    """Full R18 verification result.

    ``base`` is the r14 attestation-level result (body hash + signature).
    The R18-specific fields check symlink containment and per-file body
    hashes. ``ok`` is the conjunction of all three.
    """

    ok: bool
    reason: str
    base: VerificationResult
    symlinks_contained: bool
    file_hashes_match: bool
    offending_paths: list[str] = field(default_factory=list)
    unsafe_legacy_reason: str | None = None


def unsafe_legacy_snapshot_reason(snapshot_dir: Path) -> str | None:
    """Describe unsafe legacy hashes-only snapshots without exposing paths."""
    try:
        inventory = inventory_tree(
            snapshot_dir,
            capture_paths=frozenset({"SNAPSHOT.json"}),
            max_capture_bytes=_MAX_MANIFEST_BYTES,
        )
    except (FileNotFoundError, SymlinkEscapeError, ValueError):
        return None
    return _unsafe_legacy_reason_from_inventory(inventory)


def _unsafe_legacy_reason_from_inventory(inventory: TreeInventory) -> str | None:
    manifest_entry = inventory.entries.get("SNAPSHOT.json")
    if manifest_entry is None or manifest_entry.body is None:
        return None
    try:
        manifest = json.loads(manifest_entry.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    has_publishable_bodies = any(
        path.startswith("export/traces/") and entry.kind == "file"
        for path, entry in inventory.entries.items()
    )
    if (
        isinstance(manifest, dict)
        and manifest.get("mode") == "hashes-only"
        and has_publishable_bodies
    ):
        return (
            "legacy hashes-only snapshot contains file bodies; recreate it "
            "from the original experiment with the installed CodeProbe version"
        )
    return None


def verify_snapshot_extended(
    snapshot_dir: Path,
    signing_key: str | None = None,
) -> ExtendedVerificationResult:
    """Verify attestation, containment, layout, and file hashes."""
    snapshot_dir = Path(snapshot_dir)
    try:
        inventory = inventory_tree(
            snapshot_dir,
            capture_paths=frozenset({"SNAPSHOT.json"}),
        )
    except (FileNotFoundError, SymlinkEscapeError):
        return _inventory_failure(snapshot_dir)

    unsafe_legacy_reason = _unsafe_legacy_reason_from_inventory(inventory)
    manifest, load_error = _load_manifest(inventory)
    base = _verify_attestation(manifest, load_error, signing_key)
    symlinks_ok, files_ok, symlink_offenders, file_offenders = (
        _verify_extended_integrity(inventory, manifest)
    )
    offending = list(dict.fromkeys((*symlink_offenders, *file_offenders)))
    return ExtendedVerificationResult(
        ok=base.ok and symlinks_ok and files_ok,
        reason=_verification_reason(
            base,
            symlinks_ok,
            files_ok,
            symlink_offenders,
            file_offenders,
        ),
        base=base,
        symlinks_contained=symlinks_ok,
        file_hashes_match=files_ok,
        offending_paths=offending,
        unsafe_legacy_reason=unsafe_legacy_reason,
    )


def _load_manifest(
    inventory: TreeInventory,
) -> tuple[dict[str, object] | None, str | None]:
    manifest_entry = inventory.entries.get("SNAPSHOT.json")
    if (
        manifest_entry is None
        or manifest_entry.kind != "file"
        or manifest_entry.body is None
        or len(manifest_entry.body) > _MAX_MANIFEST_BYTES
    ):
        return None, (
            f"missing or unsafe manifest: {inventory.root / 'SNAPSHOT.json'}"
        )
    return _parse_snapshot_json(manifest_entry.body)


def _verify_attestation(
    manifest: dict[str, object] | None,
    load_error: str | None,
    signing_key: str | None,
) -> VerificationResult:
    return (
        _verify_snapshot_data(manifest, signing_key=signing_key)
        if manifest is not None
        else VerificationResult(
            ok=False,
            reason=load_error or "manifest schema is invalid",
            body_sha256_matches=False,
            signature_matches=None,
        )
    )


def _verify_extended_integrity(
    inventory: TreeInventory,
    manifest: dict[str, object] | None,
) -> tuple[bool, bool, list[str], list[str]]:
    mode = manifest.get("mode") if manifest is not None else None
    extended_required = _extended_layout_required(inventory, manifest)
    symlink_offenders = _verify_symlinks(inventory, mode)
    files_ok, file_offenders = _verify_file_hashes(
        inventory,
        manifest,
        verify_publishable=extended_required,
    )
    layout_ok, layout_offenders = _verify_layout_inventory(
        inventory,
        manifest,
        required=extended_required,
    )
    combined_file_offenders = [*file_offenders, *layout_offenders]
    return (
        not symlink_offenders,
        files_ok and layout_ok,
        symlink_offenders,
        combined_file_offenders,
    )


def _verification_reason(
    base: VerificationResult,
    symlinks_ok: bool,
    files_ok: bool,
    symlink_offenders: list[str],
    file_offenders: list[str],
) -> str:
    reason_parts: list[str] = []
    if not base.ok:
        reason_parts.append(f"attestation: {base.reason}")
    if not symlinks_ok:
        reason_parts.append(
            f"symlink containment failed: {len(symlink_offenders)} offender(s)"
        )
    if not files_ok:
        reason_parts.append(f"file hash mismatch: {len(file_offenders)} offender(s)")
    return "; ".join(reason_parts) or "ok"


def _inventory_failure(snapshot_dir: Path) -> ExtendedVerificationResult:
    path = str(snapshot_dir)
    return ExtendedVerificationResult(
        ok=False,
        reason="snapshot tree cannot be inventoried securely",
        base=VerificationResult(
            ok=False,
            reason="missing or unsafe manifest",
            body_sha256_matches=False,
            signature_matches=None,
        ),
        symlinks_contained=False,
        file_hashes_match=False,
        offending_paths=[path],
    )


def _entry_path(inventory: TreeInventory, relative_path: str) -> str:
    return str(inventory.root / relative_path)


def _verify_symlinks(
    inventory: TreeInventory,
    mode: object,
) -> list[str]:
    offenders: list[str] = []
    for entry in inventory.entries.values():
        path = PurePosixPath(entry.relative_path)
        if (
            mode == "hashes-only"
            and entry.kind == "file"
            and path.parts
            and path.parts[0] == "traces"
        ):
            offenders.append(_entry_path(inventory, entry.relative_path))
            continue
        if entry.kind != "symlink":
            continue
        target = PurePosixPath(entry.target or "")
        combined = posixpath.normpath((path.parent / target).as_posix())
        combined_path = PurePosixPath(combined)
        expected = (
            PurePosixPath("export", "traces", *path.parts[1:])
            if mode == "hashes-only"
            and len(path.parts) >= 3
            and path.parts[0] == "traces"
            else None
        )
        target_entry = inventory.entries.get(combined)
        if (
            target.is_absolute()
            or combined_path.is_absolute()
            or not combined_path.parts
            or combined_path.parts[0] == ".."
            or expected is None
            or combined_path != expected
            or target_entry is None
            or target_entry.kind != "directory"
        ):
            offenders.append(_entry_path(inventory, entry.relative_path))
    return offenders


def _verify_file_hashes(
    inventory: TreeInventory,
    manifest: dict[str, object] | None,
    *,
    verify_publishable: bool,
) -> tuple[bool, list[str]]:
    """Recompute sha256 for every file body referenced by the manifest.

    The manifest's ``files[].path`` is always relative to the snapshot's
    source directory. The on-disk body (when present) lives under
    ``snapshot_dir/files/<path>``.

    Content-bearing snapshots must provide every declared ``files/`` body and
    its post-redaction hash. Trial bodies copied into ``traces/`` and
    ``export/traces/`` must be exact copies of those authenticated bodies.
    Missing, extra, unsafe, or changed bodies fail closed.
    """
    manifest_path = inventory.root / "SNAPSHOT.json"
    if manifest is None:
        return False, [str(manifest_path)]
    mode = manifest.get("mode")
    raw_files = manifest.get("files")
    if mode not in ("hashes-only", "contents", "secrets") or not isinstance(
        raw_files,
        list,
    ):
        return False, [str(manifest_path)]
    files_dir = inventory.root / "files"
    if mode == "hashes-only":
        if "files" in inventory.entries:
            return False, [str(files_dir)]
        if not verify_publishable:
            return True, []
        hashes_only_offenders = _verify_publishable_tree(
            inventory,
            "export/traces",
            {},
        )
        return not hashes_only_offenders, hashes_only_offenders

    files_root = inventory.entries.get("files")
    if files_root is None or files_root.kind != "directory":
        return False, [str(files_dir)]
    bodies, body_offenders = _subtree_hashes(inventory, "files")
    offenders: list[str] = []
    offenders.extend(body_offenders)
    expected_publishable: dict[str, str] = {}
    expected_body_paths: set[str] = set()
    for entry in raw_files:
        if not isinstance(entry, dict):
            offenders.append(str(manifest_path))
            continue
        rel = entry.get("path")
        expected_src = entry.get("sha256")
        expected_redacted = entry.get("redacted_body_sha256")
        redacted_body = entry.get("redacted_body")
        if not isinstance(rel, str) or not isinstance(expected_src, str):
            offenders.append(str(rel))
            continue
        relative_path = PurePosixPath(rel)
        if relative_path.is_absolute() or any(
            part in ("", ".", "..") for part in relative_path.parts
        ):
            offenders.append(rel)
            continue
        candidate = files_dir / rel
        actual = bodies.get(rel)
        if actual is None:
            offenders.append(str(candidate))
            continue
        if (
            redacted_body != f"files/{rel}"
            or not isinstance(expected_redacted, str)
            or not expected_redacted
        ):
            offenders.append(str(candidate))
            continue
        if actual != expected_redacted:
            offenders.append(str(candidate))
            continue
        expected_body_paths.add(rel)
        parts = relative_path.parts
        if len(parts) >= 3 and parts[0] not in {
            "SNAPSHOT.json",
            "export",
            "files",
            "summary",
            "traces",
        }:
            trial_path = PurePosixPath(*parts).as_posix()
            expected_publishable[trial_path] = actual

    for extra_body in sorted(set(bodies) - expected_body_paths):
        offenders.append(str(files_dir / extra_body))
    if verify_publishable:
        offenders.extend(
            _verify_publishable_tree(
                inventory,
                "export/traces",
                expected_publishable,
            )
        )
        offenders.extend(
            _verify_publishable_tree(
                inventory,
                "traces",
                expected_publishable,
            )
        )

    return len(offenders) == 0, offenders


def _verify_publishable_tree(
    inventory: TreeInventory,
    root: str,
    expected_hashes: dict[str, str],
) -> list[str]:
    root_entry = inventory.entries.get(root)
    if root_entry is None or root_entry.kind != "directory":
        return [_entry_path(inventory, root)]
    actual, offenders = _subtree_hashes(inventory, root)
    root_path = inventory.root / root
    offenders = [
        *offenders,
        *(
            str(root_path / relative)
            for relative in sorted(set(actual) | set(expected_hashes))
            if actual.get(relative) != expected_hashes.get(relative)
        ),
    ]
    return offenders


def _subtree_hashes(
    inventory: TreeInventory,
    root: str,
) -> tuple[dict[str, str], list[str]]:
    prefix = f"{root}/"
    hashes: dict[str, str] = {}
    offenders: list[str] = []
    for entry in inventory.entries.values():
        if not entry.relative_path.startswith(prefix):
            continue
        relative = entry.relative_path.removeprefix(prefix)
        if entry.kind == "file" and entry.sha256 is not None:
            hashes[relative] = entry.sha256
        elif entry.kind not in ("directory", "file"):
            offenders.append(_entry_path(inventory, entry.relative_path))
    return hashes, offenders


def _extended_layout_required(
    inventory: TreeInventory,
    manifest: dict[str, object] | None,
) -> bool:
    if manifest is not None and any(
        key in manifest
        for key in (
            "schema_version",
            "created_at",
            "dependencies",
            "layout",
        )
    ):
        return True
    return any(
        entry.relative_path.split("/", 1)[0] in {"summary", "traces", "export"}
        for entry in inventory.entries.values()
    )


def _verify_extended_schema(
    inventory: TreeInventory,
    manifest: dict[str, object] | None,
) -> list[str]:
    manifest_path = _entry_path(inventory, "SNAPSHOT.json")
    if (
        manifest is None
        or manifest.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
        or not isinstance(manifest.get("created_at"), str)
        or not isinstance(manifest.get("dependencies"), dict)
        or not isinstance(manifest.get("layout"), list)
    ):
        return [manifest_path]
    return []


def _verify_layout_inventory(
    inventory: TreeInventory,
    manifest: dict[str, object] | None,
    *,
    required: bool,
) -> tuple[bool, list[str]]:
    if not required:
        return True, []
    schema_offenders = _verify_extended_schema(inventory, manifest)
    raw_layout = manifest.get("layout") if manifest is not None else None
    if not isinstance(raw_layout, list):
        return False, schema_offenders or [
            _entry_path(inventory, "SNAPSHOT.json")
        ]

    expected: dict[str, tuple[str, str | None]] = {}
    for raw_entry in raw_layout:
        if not isinstance(raw_entry, dict):
            return False, [_entry_path(inventory, "SNAPSHOT.json")]
        path = raw_entry.get("path")
        kind = raw_entry.get("kind")
        evidence = (
            raw_entry.get("sha256")
            if kind == "file"
            else raw_entry.get("target")
            if kind == "symlink"
            else None
        )
        if not isinstance(path, str):
            return False, [_entry_path(inventory, "SNAPSHOT.json")]
        expected_path = PurePosixPath(path)
        if (
            expected_path.is_absolute()
            or not expected_path.parts
            or any(part in ("", ".", "..") for part in expected_path.parts)
            or kind not in ("directory", "file", "symlink")
            or (kind in ("file", "symlink") and not isinstance(evidence, str))
            or path in expected
        ):
            return False, [_entry_path(inventory, "SNAPSHOT.json")]
        expected[path] = (kind, evidence)

    actual = {
        entry.relative_path: (
            entry.kind,
            entry.sha256
            if entry.kind == "file"
            else entry.target
            if entry.kind == "symlink"
            else None,
        )
        for entry in inventory.entries.values()
        if entry.relative_path != "SNAPSHOT.json"
    }
    offenders = [
        *schema_offenders,
        *(
            _entry_path(inventory, path)
            for path in sorted(set(actual) | set(expected))
            if expected.get(path) != actual.get(path)
        ),
    ]
    return not offenders, offenders
