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
    CanaryResult,
    load_canary_proof,
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

__all__ = [
    "Attestation",
    "CANARY_DEFAULT",
    "CanaryFailed",
    "CanaryGate",
    "CanaryResult",
    "DEFAULT_PATTERNS",
    "FileEntry",
    "Finding",
    "GitleaksScanner",
    "MockScanner",
    "PatternScanner",
    "PUBLISHABLE_DEFAULT",
    "RedactionMode",
    "Scanner",
    "ScannerUnavailable",
    "SnapshotManifest",
    "TrufflehogScanner",
    "VerificationResult",
    "load_canary_proof",
    "redact",
    "verify_snapshot",
    "write_snapshot",
]
