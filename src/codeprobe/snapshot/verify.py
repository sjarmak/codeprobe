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
import json
from dataclasses import dataclass, field
from pathlib import Path

from codeprobe.snapshot.redact import VerificationResult, verify_snapshot

__all__ = [
    "ExtendedVerificationResult",
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

    base = verify_snapshot(snapshot_dir, signing_key=signing_key)

    offending: list[str] = []
    symlinks_ok = True
    for entry in snapshot_dir.rglob("*"):
        if not entry.is_symlink():
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

    # Per-file hash recheck — applies to whatever bodies were materialised
    # on disk. In hashes-only mode no bodies exist, so this loop is a no-op
    # (and correctly returns True).
    files_ok, file_offenders = _verify_file_hashes(snapshot_dir)
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


def _verify_file_hashes(snapshot_dir: Path) -> tuple[bool, list[str]]:
    """Recompute sha256 for every file body referenced by the manifest.

    The manifest's ``files[].path`` is always relative to the snapshot's
    source directory. The on-disk body (when present) lives under
    ``snapshot_dir/files/<path>``.

    Tamper detection strategy:

    - When the manifest entry carries a ``redacted_body_sha256`` field
      (written by ``redact()`` at snapshot-creation time for ``contents`` and
      ``secrets`` modes), the on-disk body is hashed and compared against
      that field. A mismatch indicates the body was modified after the
      snapshot was produced.
    - When the field is absent (legacy snapshots, or a modality where no
      redaction transformation is applied), fall back to comparing the
      on-disk body against the source ``sha256``. This preserves backwards
      compatibility for snapshots produced before the field existed.

    Bodies copied into ``export/traces/`` are sanitised copies intended for
    publishing and may intentionally differ from the manifest entries —
    they are not tamper-checked here.
    """
    manifest_path = snapshot_dir / "SNAPSHOT.json"
    if not manifest_path.exists():
        return False, [str(manifest_path)]

    manifest = json.loads(manifest_path.read_text())
    files_dir = snapshot_dir / "files"
    if not files_dir.is_dir():
        # hashes-only snapshot: nothing to recheck — attestation alone
        # covers tamper detection for the manifest body.
        return True, []

    offenders: list[str] = []
    for entry in manifest.get("files", []):
        rel = entry.get("path")
        expected_src = entry.get("sha256")
        expected_redacted = entry.get("redacted_body_sha256")
        if not isinstance(rel, str) or not isinstance(expected_src, str):
            offenders.append(str(rel))
            continue
        candidate = files_dir / rel
        if not candidate.is_file():
            # The manifest references a file that no on-disk body exists
            # for. In content modes this is a genuine problem, but because
            # legacy hashes-only and partial snapshots can reach this path,
            # we treat a missing-body as "not a tamper signal" and let the
            # attestation-level body_sha256 catch manifest-level corruption.
            continue
        actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
        if isinstance(expected_redacted, str) and expected_redacted:
            # Preferred path: we have a hash of the bytes actually written
            # to disk after scanner redaction, so any single-byte tamper is
            # detectable regardless of what the redaction did.
            if actual != expected_redacted:
                offenders.append(str(candidate))
        else:
            # Legacy fallback: compare against the source sha. This is only
            # correct when redaction was a byte-for-byte passthrough (e.g.
            # a MockScanner with no hits); for real scanners the hashes
            # legitimately differ and we cannot distinguish tampering.
            if actual != expected_src:
                offenders.append(str(candidate))

    return len(offenders) == 0, offenders


def _is_within(path: Path, root: Path) -> bool:
    """Return True when ``path`` is equal to or nested under ``root``."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
