"""Snapshot redaction pipeline.

The public entry point is :func:`redact`. It walks ``source_dir``, writes a
``SNAPSHOT.json`` manifest to ``out_dir``, and — depending on ``mode`` — may
also copy file bodies to ``out_dir/files/`` after running them through the
configured secret scanner.

Modes (see ``docs/SNAPSHOT_REDACTION.md`` for the full capability matrix):

- ``hashes-only`` (public default): only ``sha256 + size`` per file. No bodies.
- ``contents``: bodies copied, but every file is piped through
  ``scanner.redact(bytes)``. Requires an explicit opt-in at the CLI boundary.
- ``secrets``: same as ``contents``, AND requires a pre-publish canary gate
  pass (either inline or supplied via ``canary_proof``).

Attestation:

- The manifest is signed via HMAC-SHA256 when a signing key is available
  (arg ``signing_key`` or env ``CODEPROBE_SIGNING_KEY``).
- If no key is available, the manifest is written with an
  ``attestation.kind='unsigned'`` marker and the body sha256 only.
- Production deployments MUST supply a key. The unsigned mode exists for
  offline / local-only previews.

No LLM is invoked from this module. Verified by
``tests/snapshot/test_canary_gate.py`` via ``grep -R`` across
``src/codeprobe/snapshot/``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from codeprobe.snapshot.canary import (
    CanaryGate,
    CanaryResult,
    validate_canary_proof,
)
from codeprobe.snapshot.safe_io import (
    SecureOutputDirectory,
    SourceFile,
    SymlinkEscapeError,
    read_regular_file,
    read_source_files,
    staged_output_directory,
)
from codeprobe.snapshot.scanners import (
    PatternScanner,
    Scanner,
    ScannerError,
    pinned_scanner,
)

RedactionMode = Literal["hashes-only", "contents", "secrets"]

# The public default — codeprobe snapshot create uses this when the caller
# omits --redact. See docs/SNAPSHOT_REDACTION.md.
PUBLISHABLE_DEFAULT: RedactionMode = "hashes-only"

SIGNING_KEY_ENV = "CODEPROBE_SIGNING_KEY"
_MANIFEST_NAME = "SNAPSHOT.json"
_FILES_SUBDIR = "files"
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class FileEntry:
    """One row in the manifest's file list."""

    path: str
    sha256: str
    size: int
    redacted_body: str | None = None  # relative path under out_dir/files/ if present
    # sha256 of the bytes actually written to ``redacted_body`` (post-scanner).
    # Only populated in content-bearing modes; absent / None in hashes-only
    # mode or when the file body was not materialised on disk. Verifiers use
    # this to distinguish legitimate redaction diffs from post-write tampering.
    redacted_body_sha256: str | None = None


@dataclass(frozen=True)
class LayoutEntry:
    """One authenticated path in an extended snapshot output tree."""

    path: str
    kind: Literal["directory", "file", "symlink"]
    sha256: str | None = None
    target: str | None = None


@dataclass(frozen=True)
class Attestation:
    """HMAC or unsigned attestation stored on the manifest."""

    kind: Literal["hmac-sha256", "unsigned"]
    signature: str
    body_sha256: str
    redaction_mode: str
    scanner_name: str | None
    canary: str | None
    timestamp: str


@dataclass
class SnapshotManifest:
    """In-memory representation of ``SNAPSHOT.json``."""

    mode: RedactionMode
    source: str
    files: list[FileEntry] = field(default_factory=list)
    attestation: Attestation | None = None
    canary_result: dict[str, object] | None = None
    layout: list[LayoutEntry] | None = None

    def to_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "mode": self.mode,
            "source": self.source,
            "files": [asdict(f) for f in self.files],
        }
        if self.canary_result is not None:
            body["canary_result"] = self.canary_result
        if self.layout is not None:
            body["layout"] = [asdict(entry) for entry in self.layout]
        if self.attestation is not None:
            body["attestation"] = asdict(self.attestation)
        return body


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of ``verify_snapshot``."""

    ok: bool
    reason: str
    body_sha256_matches: bool
    signature_matches: bool | None  # None if unsigned


@dataclass(frozen=True)
class _PreparedSnapshot:
    manifest: SnapshotManifest
    materialized_bodies: tuple[tuple[str, bytes], ...]
    source_files: tuple[SourceFile, ...]
    source_directories: tuple[str, ...]


def redact(
    source_dir: Path,
    mode: RedactionMode,
    out_dir: Path,
    scanner: Scanner | None = None,
    signing_key: str | None = None,
    canary_proof: CanaryResult | None = None,
    allow_source_in_export: bool = False,
) -> SnapshotManifest:
    """Walk ``source_dir`` and emit a snapshot manifest under ``out_dir``.

    Parameters
    ----------
    source_dir:
        Directory to snapshot.
    mode:
        Redaction mode (see module docstring).
    out_dir:
        Directory to write ``SNAPSHOT.json`` (and, for content modes, a
        ``files/`` subtree) into. Created if missing.
    scanner:
        Scanner used for the canary gate (all modes) and for redacting file
        bodies (content modes). Defaults to :class:`PatternScanner` if not
        provided and the mode actually needs a scanner.
    signing_key:
        Overrides ``CODEPROBE_SIGNING_KEY``. If neither is set, the manifest
        is written with ``attestation.kind='unsigned'``.
    canary_proof:
        Pre-computed canary result. Required for ``mode='secrets'`` unless
        the caller supplies a scanner and accepts an inline gate run.
    allow_source_in_export:
        Must be True for ``mode in {"contents", "secrets"}``. The CLI
        enforces this flag, but the library refuses too so programmatic
        callers can't bypass it.

    Returns
    -------
    SnapshotManifest
        The manifest also written to disk as ``SNAPSHOT.json``.
    """

    prepared = _prepare_snapshot(
        source_dir=source_dir,
        mode=mode,
        out_dir=out_dir,
        scanner=scanner,
        signing_key=signing_key,
        canary_proof=canary_proof,
        allow_source_in_export=allow_source_in_export,
    )
    prepared = replace(
        prepared,
        source_files=(),
        source_directories=(),
    )
    with staged_output_directory(Path(out_dir)) as output:
        _write_materialized_bodies(prepared, output)
        output.ensure_path_unchanged()
        _write_snapshot_to_output(prepared.manifest, output)
        output.ensure_path_unchanged()
    return prepared.manifest


def _prepare_snapshot(
    source_dir: Path,
    mode: RedactionMode,
    out_dir: Path,
    scanner: Scanner | None = None,
    signing_key: str | None = None,
    canary_proof: CanaryResult | None = None,
    allow_source_in_export: bool = False,
) -> _PreparedSnapshot:
    """Build the manifest and transformed bodies without creating output.

    Scanner, source-containment, and rescan failures therefore occur before
    any output directory or file is created.
    """
    source_dir = Path(source_dir)
    out_dir = Path(out_dir)
    if mode not in ("hashes-only", "contents", "secrets"):
        raise ValueError(f"unknown redaction mode: {mode!r}")
    if mode in ("contents", "secrets") and not allow_source_in_export:
        raise PermissionError(
            f"mode={mode!r} requires allow_source_in_export=True. "
            f"On the CLI, pass --allow-source-in-export explicitly."
        )

    need_scanner = mode in ("contents", "secrets")
    effective_scanner = scanner if scanner is not None else (
        PatternScanner() if need_scanner else None
    )
    with pinned_scanner(effective_scanner) as captured_scanner:
        return _prepare_snapshot_with_scanner(
            source_dir=source_dir,
            mode=mode,
            out_dir=out_dir,
            effective_scanner=captured_scanner,
            signing_key=signing_key,
            canary_proof=canary_proof,
        )


def _prepare_snapshot_with_scanner(
    *,
    source_dir: Path,
    mode: RedactionMode,
    out_dir: Path,
    effective_scanner: Scanner | None,
    signing_key: str | None,
    canary_proof: CanaryResult | None,
) -> _PreparedSnapshot:
    """Capture and transform source bytes with one pinned scanner runtime."""
    need_scanner = mode in ("contents", "secrets")

    # contents/secrets modes both copy source-derived bodies into the
    # snapshot, so both must prove that the configured scanner can actually
    # detect secrets before any body is written. BC-H-04: prior to this
    # gate being extended, ``mode="contents"`` could produce a redacted-body
    # snapshot with a silently-broken scanner.
    canary_record: CanaryResult | None = None
    if mode in ("contents", "secrets"):
        if canary_proof is not None:
            assert effective_scanner is not None
            validate_canary_proof(canary_proof, effective_scanner)
            canary_record = canary_proof
        else:
            if effective_scanner is None:
                raise PermissionError(
                    f"mode={mode!r} requires either a canary_proof or a scanner "
                    "to run the inline canary gate."
                )
            canary_record = CanaryGate(effective_scanner).require_pass_or_raise()

    source_absolute, source_files, source_directories = read_source_files(
        source_dir,
        output_dir=out_dir,
    )
    files: list[FileEntry] = []
    materialized_bodies: list[tuple[str, bytes]] = []
    for source_file in source_files:
        rel = source_file.relative_path
        body = source_file.body
        sha = hashlib.sha256(body).hexdigest()
        entry = FileEntry(path=rel, sha256=sha, size=len(body))

        if need_scanner:
            assert effective_scanner is not None
            redacted = effective_scanner.redact(body)
            residual_findings = effective_scanner.scan(redacted)
            if residual_findings:
                scanner_name = getattr(effective_scanner, "name", "unknown")
                raise ScannerError(
                    f"{scanner_name} still detected findings after redaction"
                )
            materialized_bodies.append(
                ((Path(_FILES_SUBDIR) / rel).as_posix(), redacted)
            )
            redacted_sha = hashlib.sha256(redacted).hexdigest()
            entry = FileEntry(
                path=rel,
                sha256=sha,
                size=len(body),
                redacted_body=(Path(_FILES_SUBDIR) / rel).as_posix(),
                redacted_body_sha256=redacted_sha,
            )
        files.append(entry)

    scanner_name = getattr(effective_scanner, "name", None) if effective_scanner else None
    manifest = SnapshotManifest(
        mode=mode,
        source=str(source_absolute),
        files=files,
        canary_result=canary_record.to_dict() if canary_record is not None else None,
    )

    attestation = _attest(
        manifest=manifest,
        signing_key=_resolve_signing_key(signing_key),
        scanner_name=scanner_name,
        canary=canary_record.canary if canary_record else None,
    )
    manifest.attestation = attestation

    return _PreparedSnapshot(
        manifest=manifest,
        materialized_bodies=tuple(materialized_bodies),
        source_files=tuple(source_files),
        source_directories=tuple(source_directories),
    )


def _write_materialized_bodies(
    prepared: _PreparedSnapshot,
    output: SecureOutputDirectory,
) -> None:
    for relative_path, body in prepared.materialized_bodies:
        output.write_bytes(relative_path, body)


def _canonical_body_bytes(manifest: SnapshotManifest) -> bytes:
    """Deterministic serialization of the manifest body (pre-signature).

    The body intentionally excludes the attestation signature field itself
    so the signature is computed over a stable payload.
    """

    payload: dict[str, object] = {
        "mode": manifest.mode,
        "source": manifest.source,
        "files": [asdict(f) for f in manifest.files],
    }
    if manifest.canary_result is not None:
        payload["canary_result"] = manifest.canary_result
    if manifest.layout is not None:
        payload["layout"] = [asdict(entry) for entry in manifest.layout]
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _resolve_signing_key(signing_key: str | None) -> str | None:
    if signing_key is not None:
        return signing_key
    env_val = os.environ.get(SIGNING_KEY_ENV)
    if env_val is None or env_val == "":
        return None
    return env_val


def _attest(
    manifest: SnapshotManifest,
    signing_key: str | None,
    scanner_name: str | None,
    canary: str | None,
) -> Attestation:
    body = _canonical_body_bytes(manifest)
    body_sha = hashlib.sha256(body).hexdigest()
    timestamp = datetime.now(UTC).isoformat()

    if signing_key:
        sig = hmac.new(
            signing_key.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        return Attestation(
            kind="hmac-sha256",
            signature=sig,
            body_sha256=body_sha,
            redaction_mode=manifest.mode,
            scanner_name=scanner_name,
            canary=canary,
            timestamp=timestamp,
        )
    return Attestation(
        kind="unsigned",
        signature="",
        body_sha256=body_sha,
        redaction_mode=manifest.mode,
        scanner_name=scanner_name,
        canary=canary,
        timestamp=timestamp,
    )


def write_snapshot(manifest: SnapshotManifest, out_dir: Path) -> Path:
    """Atomically publish ``out_dir/SNAPSHOT.json`` and return the path."""

    destination = Path(os.path.abspath(os.fspath(out_dir)))
    with staged_output_directory(destination) as output:
        _write_snapshot_to_output(manifest, output)
        output.ensure_path_unchanged()
    return destination / _MANIFEST_NAME


def _write_snapshot_to_output(
    manifest: SnapshotManifest,
    output: SecureOutputDirectory,
) -> Path:
    serialized = json.dumps(
        manifest.to_dict(),
        sort_keys=True,
        indent=2,
        separators=(",", ": "),
    ).encode()
    return output.write_bytes(_MANIFEST_NAME, serialized)


def verify_snapshot(
    snapshot_dir: Path,
    signing_key: str | None = None,
) -> VerificationResult:
    """Recompute the body hash and — if HMAC-signed — verify the signature."""

    snapshot_dir = Path(snapshot_dir)
    raw, load_error = _load_snapshot_json(snapshot_dir)
    if raw is None:
        return VerificationResult(
            ok=False,
            reason=load_error or "manifest schema is invalid",
            body_sha256_matches=False,
            signature_matches=None,
        )
    return _verify_snapshot_data(raw, signing_key=signing_key)


def _load_snapshot_json(
    snapshot_dir: Path,
) -> tuple[dict[str, object] | None, str | None]:
    """Load one bounded, no-follow manifest for all verification passes."""
    manifest_path = snapshot_dir / _MANIFEST_NAME
    try:
        manifest_body = read_regular_file(
            snapshot_dir,
            _MANIFEST_NAME,
            max_bytes=_MAX_MANIFEST_BYTES,
        )
    except (FileNotFoundError, SymlinkEscapeError):
        return None, f"missing or unsafe manifest: {manifest_path}"
    try:
        raw = json.loads(manifest_body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        return None, "manifest is malformed"
    if not isinstance(raw, dict):
        return None, "manifest schema is invalid: root must be an object"
    return raw, None


def _manifest_from_raw(raw: dict[str, object]) -> SnapshotManifest:
    mode = raw.get("mode")
    source = raw.get("source")
    raw_files = raw.get("files")
    if mode not in ("hashes-only", "contents", "secrets"):
        raise ValueError("mode is invalid")
    if not isinstance(source, str) or not isinstance(raw_files, list):
        raise ValueError("source or files field is invalid")

    files: list[FileEntry] = []
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise ValueError("files entries must be objects")
        path = raw_file.get("path")
        sha256 = raw_file.get("sha256")
        size = raw_file.get("size")
        redacted_body = raw_file.get("redacted_body")
        redacted_sha = raw_file.get("redacted_body_sha256")
        if (
            not isinstance(path, str)
            or not isinstance(sha256, str)
            or type(size) is not int
            or size < 0
            or (redacted_body is not None and not isinstance(redacted_body, str))
            or (redacted_sha is not None and not isinstance(redacted_sha, str))
        ):
            raise ValueError("files entry fields are invalid")
        files.append(
            FileEntry(
                path=path,
                sha256=sha256,
                size=size,
                redacted_body=redacted_body,
                redacted_body_sha256=redacted_sha,
            )
        )

    canary_result = raw.get("canary_result")
    if canary_result is not None and not isinstance(canary_result, dict):
        raise ValueError("canary_result is invalid")
    raw_layout = raw.get("layout")
    layout: list[LayoutEntry] | None = None
    if raw_layout is not None:
        if not isinstance(raw_layout, list):
            raise ValueError("layout is invalid")
        layout = []
        for raw_entry in raw_layout:
            if not isinstance(raw_entry, dict):
                raise ValueError("layout entry is invalid")
            layout_path = raw_entry.get("path")
            kind = raw_entry.get("kind")
            sha256 = raw_entry.get("sha256")
            target = raw_entry.get("target")
            if (
                not isinstance(layout_path, str)
                or kind not in ("directory", "file", "symlink")
                or (sha256 is not None and not isinstance(sha256, str))
                or (target is not None and not isinstance(target, str))
            ):
                raise ValueError("layout entry fields are invalid")
            layout.append(
                LayoutEntry(
                    path=layout_path,
                    kind=kind,
                    sha256=sha256,
                    target=target,
                )
            )
    return SnapshotManifest(
        mode=mode,
        source=source,
        files=files,
        canary_result=canary_result,
        layout=layout,
    )


def _verify_snapshot_data(
    raw: dict[str, object],
    *,
    signing_key: str | None,
) -> VerificationResult:
    """Verify already-loaded manifest data without reopening its path."""
    attestation = raw.get("attestation")
    if not isinstance(attestation, dict):
        return VerificationResult(
            ok=False,
            reason="manifest missing attestation block",
            body_sha256_matches=False,
            signature_matches=None,
        )

    try:
        recomputed = _manifest_from_raw(raw)
    except (TypeError, ValueError):
        return VerificationResult(
            ok=False,
            reason="manifest schema is invalid",
            body_sha256_matches=False,
            signature_matches=None,
        )
    body = _canonical_body_bytes(recomputed)
    body_sha = hashlib.sha256(body).hexdigest()
    expected_body = str(attestation.get("body_sha256", ""))
    body_ok = hmac.compare_digest(body_sha, expected_body)

    kind = attestation.get("kind")
    sig_ok: bool | None
    if kind == "hmac-sha256":
        key = _resolve_signing_key(signing_key)
        if key is None:
            return VerificationResult(
                ok=False,
                reason="manifest is HMAC-signed but no signing key is configured",
                body_sha256_matches=body_ok,
                signature_matches=None,
            )
        expected_sig = hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()
        sig_ok = hmac.compare_digest(
            expected_sig, str(attestation.get("signature", ""))
        )
    elif kind == "unsigned":
        sig_ok = None
    else:
        return VerificationResult(
            ok=False,
            reason=f"unknown attestation.kind={kind!r}",
            body_sha256_matches=body_ok,
            signature_matches=None,
        )

    ok = body_ok and (sig_ok is not False)
    reason = "ok" if ok else "attestation mismatch"
    return VerificationResult(
        ok=ok,
        reason=reason,
        body_sha256_matches=body_ok,
        signature_matches=sig_ok,
    )


__all__ = [
    "Attestation",
    "FileEntry",
    "LayoutEntry",
    "PUBLISHABLE_DEFAULT",
    "RedactionMode",
    "SIGNING_KEY_ENV",
    "SnapshotManifest",
    "SymlinkEscapeError",
    "VerificationResult",
    "redact",
    "verify_snapshot",
    "write_snapshot",
]
