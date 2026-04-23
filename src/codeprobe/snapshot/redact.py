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
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from codeprobe.snapshot.canary import CanaryGate, CanaryResult
from codeprobe.snapshot.scanners import PatternScanner, Scanner

RedactionMode = Literal["hashes-only", "contents", "secrets"]

# The public default — codeprobe snapshot create uses this when the caller
# omits --redact. See docs/SNAPSHOT_REDACTION.md.
PUBLISHABLE_DEFAULT: RedactionMode = "hashes-only"

SIGNING_KEY_ENV = "CODEPROBE_SIGNING_KEY"
_MANIFEST_NAME = "SNAPSHOT.json"
_FILES_SUBDIR = "files"


@dataclass(frozen=True)
class FileEntry:
    """One row in the manifest's file list."""

    path: str
    sha256: str
    size: int
    redacted_body: str | None = None  # relative path under out_dir/files/ if present


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

    def to_dict(self) -> dict[str, object]:
        body: dict[str, object] = {
            "mode": self.mode,
            "source": self.source,
            "files": [asdict(f) for f in self.files],
        }
        if self.canary_result is not None:
            body["canary_result"] = self.canary_result
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

    source_dir = Path(source_dir)
    out_dir = Path(out_dir)
    if not source_dir.exists() or not source_dir.is_dir():
        raise FileNotFoundError(
            f"snapshot source_dir does not exist or is not a directory: {source_dir}"
        )
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

    # secrets mode requires a proven canary gate.
    canary_record: CanaryResult | None = None
    if mode == "secrets":
        if canary_proof is not None:
            if not canary_proof.passed:
                raise PermissionError(
                    "mode='secrets' requires a passing canary_proof; "
                    f"received failed proof from scanner {canary_proof.scanner_name!r}"
                )
            canary_record = canary_proof
        else:
            if effective_scanner is None:
                raise PermissionError(
                    "mode='secrets' requires either a canary_proof or a scanner "
                    "to run the inline canary gate."
                )
            canary_record = CanaryGate(effective_scanner).require_pass_or_raise()

    out_dir.mkdir(parents=True, exist_ok=True)
    files_out = out_dir / _FILES_SUBDIR
    if need_scanner:
        files_out.mkdir(parents=True, exist_ok=True)

    files: list[FileEntry] = []
    for abs_path in sorted(_walk_files(source_dir)):
        rel = abs_path.relative_to(source_dir).as_posix()
        # Always skip re-ingesting our own output if the user passed out_dir
        # inside source_dir.
        try:
            abs_path.relative_to(out_dir.resolve())
            continue
        except ValueError:
            pass

        body = abs_path.read_bytes()
        sha = hashlib.sha256(body).hexdigest()
        entry = FileEntry(path=rel, sha256=sha, size=len(body))

        if need_scanner:
            assert effective_scanner is not None
            redacted = effective_scanner.redact(body)
            target = files_out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(redacted)
            entry = FileEntry(
                path=rel,
                sha256=sha,
                size=len(body),
                redacted_body=(Path(_FILES_SUBDIR) / rel).as_posix(),
            )
        files.append(entry)

    scanner_name = getattr(effective_scanner, "name", None) if effective_scanner else None
    manifest = SnapshotManifest(
        mode=mode,
        source=str(source_dir.resolve()),
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

    write_snapshot(manifest, out_dir)
    return manifest


def _walk_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file():
            out.append(p)
    return out


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
    timestamp = datetime.now(timezone.utc).isoformat()

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
    """Serialize ``manifest`` to ``out_dir/SNAPSHOT.json`` and return the path."""

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / _MANIFEST_NAME
    dest.write_text(
        json.dumps(manifest.to_dict(), sort_keys=True, indent=2, separators=(",", ": "))
    )
    return dest


def verify_snapshot(
    snapshot_dir: Path,
    signing_key: str | None = None,
) -> VerificationResult:
    """Recompute the body hash and — if HMAC-signed — verify the signature."""

    snapshot_dir = Path(snapshot_dir)
    manifest_path = snapshot_dir / _MANIFEST_NAME
    if not manifest_path.exists():
        return VerificationResult(
            ok=False,
            reason=f"missing manifest: {manifest_path}",
            body_sha256_matches=False,
            signature_matches=None,
        )
    raw = json.loads(manifest_path.read_text())
    attestation = raw.get("attestation")
    if not isinstance(attestation, dict):
        return VerificationResult(
            ok=False,
            reason="manifest missing attestation block",
            body_sha256_matches=False,
            signature_matches=None,
        )

    files = [
        FileEntry(
            path=str(f.get("path", "")),
            sha256=str(f.get("sha256", "")),
            size=int(f.get("size", 0)),
            redacted_body=(
                str(f["redacted_body"]) if f.get("redacted_body") is not None else None
            ),
        )
        for f in raw.get("files", [])
    ]
    recomputed = SnapshotManifest(
        mode=str(raw.get("mode", "hashes-only")),  # type: ignore[arg-type]
        source=str(raw.get("source", "")),
        files=files,
        canary_result=raw.get("canary_result"),
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
    "PUBLISHABLE_DEFAULT",
    "RedactionMode",
    "SIGNING_KEY_ENV",
    "SnapshotManifest",
    "VerificationResult",
    "redact",
    "verify_snapshot",
    "write_snapshot",
]


# shutil is imported to keep the redact() public surface future-proof for
# callers that may want to chain into shutil.copytree for a raw (no-scanner)
# copy. Without this reference, mypy/ruff flag the import as unused.
_ = shutil
