"""codeprobe snapshot — shareable snapshots with deterministic redaction.

Public surface:

- ``redact(source_dir, mode, out_dir, ...)`` — produce a SNAPSHOT.json manifest
  (and optionally a redacted file tree) from an experiment directory.
- ``CanaryGate`` — pre-publish gate that forces the configured secret scanner
  to prove it would catch a planted canary before any secret-bearing mode
  (``contents`` / ``secrets``) is allowed to run.
- ``Scanner`` protocol + builtin scanners (``PatternScanner``,
  ``GitleaksScanner``, ``TrufflehogScanner``, ``MockScanner``).
- ``verify_snapshot(snapshot_dir)`` — recompute hashes and verify the signed
  attestation stored on disk.

The default redaction mode exposed by ``codeprobe snapshot create`` is
``hashes-only``: every file in the source directory is recorded as a
``sha256 + size`` entry, but no bodies are ever copied into the snapshot.
This is the only mode the publishable CLI surface accepts without an explicit
``--allow-source-in-export`` opt-in.

No LLM is invoked anywhere in this module (tested by
``tests/snapshot/test_canary_gate.py`` via a repo grep).
"""

from codeprobe.snapshot.canary import (
    CANARY_DEFAULT,
    CanaryFailed,
    CanaryGate,
    CanaryProofInvalid,
    CanaryResult,
    load_canary_proof,
)
from codeprobe.snapshot.create import (
    CsbLayout,
    SymlinkEscapeError,
    create_snapshot,
    preflight_symlink_containment,
)
from codeprobe.snapshot.manifest import (
    SNAPSHOT_SCHEMA_VERSION,
    Dependencies,
    ExtendedManifest,
    build_extended_manifest,
    collect_dependencies,
    manifest_to_json_dict,
    write_extended_manifest,
)
from codeprobe.snapshot.redact import (
    PUBLISHABLE_DEFAULT,
    Attestation,
    FileEntry,
    RedactionMode,
    SnapshotManifest,
    VerificationResult,
    redact,
    verify_snapshot,
    write_snapshot,
)
from codeprobe.snapshot.scanners import (
    DEFAULT_PATTERNS,
    Finding,
    GitleaksScanner,
    MockScanner,
    PatternScanner,
    Scanner,
    ScannerUnavailable,
    TrufflehogScanner,
)
from codeprobe.snapshot.verify import (
    ExtendedVerificationResult,
    verify_snapshot_extended,
)

__all__ = [
    "Attestation",
    "CANARY_DEFAULT",
    "CanaryFailed",
    "CanaryGate",
    "CanaryProofInvalid",
    "CanaryResult",
    "CsbLayout",
    "DEFAULT_PATTERNS",
    "Dependencies",
    "ExtendedManifest",
    "ExtendedVerificationResult",
    "FileEntry",
    "Finding",
    "GitleaksScanner",
    "MockScanner",
    "PatternScanner",
    "PUBLISHABLE_DEFAULT",
    "RedactionMode",
    "SNAPSHOT_SCHEMA_VERSION",
    "Scanner",
    "ScannerUnavailable",
    "SnapshotManifest",
    "SymlinkEscapeError",
    "TrufflehogScanner",
    "VerificationResult",
    "build_extended_manifest",
    "collect_dependencies",
    "create_snapshot",
    "load_canary_proof",
    "manifest_to_json_dict",
    "preflight_symlink_containment",
    "redact",
    "verify_snapshot",
    "verify_snapshot_extended",
    "write_extended_manifest",
    "write_snapshot",
]
