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

import hashlib
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from codeprobe.snapshot.fairness import (
    FairnessLeak,
    FairnessResult,
    check_fairness,
)
from codeprobe.snapshot.redact import (
    VerificationResult,
    _load_snapshot_json,
    _verify_snapshot_data,
)
from codeprobe.snapshot.safe_io import (
    MAX_SOURCE_CAPTURE_BYTES,
    SymlinkEscapeError,
    read_regular_file,
    read_source_files,
)

__all__ = [
    "ExtendedVerificationResult",
    "FairnessLeak",
    "FairnessResult",
    "check_fairness",
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


def verify_snapshot_extended(
    snapshot_dir: Path,
    signing_key: str | None = None,
) -> ExtendedVerificationResult:
    """Verify a snapshot's attestation, symlink containment, and file hashes.

    **Symlink containment** — every symlink anywhere in the snapshot must be
    relative and must resolve to a path still inside the snapshot directory.
    Absolute-path symlinks are always offenders because they break
    relocation regardless of target; relative links that escape via
    ``../`` traversal are offenders too. The ``traces/`` subtree
    (per the CSB layout) contains relative symlinks pointing into
    ``export/traces/`` inside the same snapshot, which keeps the whole
    tree self-contained and relocatable.
    """
    snapshot_dir = Path(snapshot_dir)
    snapshot_resolved = snapshot_dir.resolve()

    manifest, load_error = _load_snapshot_json(snapshot_dir)
    base = (
        _verify_snapshot_data(manifest, signing_key=signing_key)
        if manifest is not None
        else VerificationResult(
            ok=False,
            reason=load_error or "manifest schema is invalid",
            body_sha256_matches=False,
            signature_matches=None,
        )
    )

    offending: list[str] = []
    symlinks_ok = True
    mode = manifest.get("mode") if manifest is not None else None
    for entry in snapshot_dir.rglob("*"):
        if not entry.is_symlink():
            if (
                mode == "hashes-only"
                and entry.is_file()
                and entry.is_relative_to(snapshot_dir / "traces")
            ):
                symlinks_ok = False
                offending.append(str(entry))
            continue
        link_target = entry.readlink()
        if link_target.is_absolute():
            symlinks_ok = False
            offending.append(str(entry))
            continue
        try:
            target_resolved = (entry.parent / link_target).resolve()
        except OSError:
            symlinks_ok = False
            offending.append(str(entry))
            continue
        if not _is_within(target_resolved, snapshot_resolved):
            symlinks_ok = False
            offending.append(str(entry))
            continue
        relative = entry.relative_to(snapshot_dir)
        parts = relative.parts
        expected_target = (
            snapshot_dir / "export" / "traces" / Path(*parts[1:])
            if mode == "hashes-only"
            and len(parts) >= 3
            and parts[0] == "traces"
            else None
        )
        if (
            expected_target is None
            or target_resolved != expected_target.resolve()
            or not expected_target.is_dir()
        ):
            symlinks_ok = False
            offending.append(str(entry))

    # Per-file hash recheck — applies to whatever bodies were materialised
    # on disk. In hashes-only mode no bodies exist, so this loop is a no-op
    # (and correctly returns True).
    files_ok, file_offenders = _verify_file_hashes(snapshot_dir, manifest)
    layout_ok, layout_offenders = _verify_layout_inventory(snapshot_dir, manifest)
    files_ok = files_ok and layout_ok
    file_offenders.extend(layout_offenders)
    offending.extend(file_offenders)

    ok = base.ok and symlinks_ok and files_ok
    reason_parts: list[str] = []
    if not base.ok:
        reason_parts.append(f"attestation: {base.reason}")
    if not symlinks_ok:
        reason_parts.append(f"symlink containment failed: {len(offending)} offender(s)")
    if not files_ok:
        reason_parts.append(f"file hash mismatch: {len(file_offenders)} offender(s)")
    reason = "ok" if ok else "; ".join(reason_parts) or "failed"

    return ExtendedVerificationResult(
        ok=ok,
        reason=reason,
        base=base,
        symlinks_contained=symlinks_ok,
        file_hashes_match=files_ok,
        offending_paths=offending,
    )


def _verify_file_hashes(
    snapshot_dir: Path,
    manifest: dict[str, object] | None,
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
    manifest_path = snapshot_dir / "SNAPSHOT.json"
    if manifest is None:
        return False, [str(manifest_path)]
    mode = manifest.get("mode")
    raw_files = manifest.get("files")
    if mode not in ("hashes-only", "contents", "secrets") or not isinstance(
        raw_files,
        list,
    ):
        return False, [str(manifest_path)]
    files_dir = snapshot_dir / "files"
    if mode == "hashes-only":
        if files_dir.exists():
            return False, [str(files_dir)]
        hashes_only_offenders = _verify_publishable_tree(
            snapshot_dir / "export" / "traces",
            {},
        )
        return not hashes_only_offenders, hashes_only_offenders

    try:
        _, body_files, _ = read_source_files(files_dir)
    except (FileNotFoundError, SymlinkEscapeError):
        return False, [str(files_dir)]
    bodies = {source_file.relative_path: source_file.body for source_file in body_files}
    offenders: list[str] = []
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
        body = bodies.get(rel)
        if body is None:
            offenders.append(str(candidate))
            continue
        if (
            redacted_body != f"files/{rel}"
            or not isinstance(expected_redacted, str)
            or not expected_redacted
        ):
            offenders.append(str(candidate))
            continue
        actual = hashlib.sha256(body).hexdigest()
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
    offenders.extend(
        _verify_publishable_tree(
            snapshot_dir / "export" / "traces",
            expected_publishable,
        )
    )
    offenders.extend(
        _verify_publishable_tree(
            snapshot_dir / "traces",
            expected_publishable,
        )
    )

    return len(offenders) == 0, offenders


def _verify_publishable_tree(
    root: Path,
    expected_hashes: dict[str, str],
) -> list[str]:
    try:
        _, files, _ = read_source_files(root)
    except (FileNotFoundError, SymlinkEscapeError):
        return [str(root)]
    actual = {
        source_file.relative_path: hashlib.sha256(source_file.body).hexdigest()
        for source_file in files
    }
    offenders = [
        str(root / relative)
        for relative in sorted(set(actual) | set(expected_hashes))
        if actual.get(relative) != expected_hashes.get(relative)
    ]
    return offenders


def _verify_layout_inventory(
    snapshot_dir: Path,
    manifest: dict[str, object] | None,
) -> tuple[bool, list[str]]:
    if manifest is None or "schema_version" not in manifest:
        return True, []
    raw_layout = manifest.get("layout")
    if not isinstance(raw_layout, list):
        return False, [str(snapshot_dir / "SNAPSHOT.json")]

    expected: dict[str, tuple[str, str | None]] = {}
    for raw_entry in raw_layout:
        if not isinstance(raw_entry, dict):
            return False, [str(snapshot_dir / "SNAPSHOT.json")]
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
            return False, [str(snapshot_dir / "SNAPSHOT.json")]
        expected_path = PurePosixPath(path)
        if (
            expected_path.is_absolute()
            or not expected_path.parts
            or any(part in ("", ".", "..") for part in expected_path.parts)
            or kind not in ("directory", "file", "symlink")
            or (kind in ("file", "symlink") and not isinstance(evidence, str))
            or path in expected
        ):
            return False, [str(snapshot_dir / "SNAPSHOT.json")]
        expected[path] = (kind, evidence)

    actual: dict[str, tuple[str, str | None]] = {}
    offenders: list[str] = []
    try:
        entries = list(snapshot_dir.rglob("*"))
    except OSError:
        return False, [str(snapshot_dir)]
    for entry in entries:
        actual_path = entry.relative_to(snapshot_dir).as_posix()
        if actual_path == "SNAPSHOT.json":
            continue
        try:
            if entry.is_symlink():
                actual[actual_path] = ("symlink", entry.readlink().as_posix())
            elif entry.is_dir():
                actual[actual_path] = ("directory", None)
            elif entry.is_file():
                body = read_regular_file(
                    snapshot_dir,
                    actual_path,
                    max_bytes=MAX_SOURCE_CAPTURE_BYTES,
                )
                actual[actual_path] = (
                    "file",
                    hashlib.sha256(body).hexdigest(),
                )
            else:
                actual[actual_path] = ("special", None)
        except (OSError, SymlinkEscapeError):
            offenders.append(str(entry))

    for path in sorted(set(expected) | set(actual)):
        if expected.get(path) != actual.get(path):
            offenders.append(str(snapshot_dir / path))
    return not offenders, offenders


def _is_within(path: Path, root: Path) -> bool:
    """Return True when ``path`` is equal to or nested under ``root``."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
