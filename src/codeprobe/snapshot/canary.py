"""Pre-publish canary gate.

Before any ``--redact=secrets`` (or, when explicitly opted in, ``--redact=contents``)
snapshot is written, the configured scanner must *prove* it would catch a
known canary string. If the scanner misses the canary, the gate refuses and
the snapshot creation aborts. This protects users against silently-broken
scanner installations, mis-configured rule sets, or empty pattern lists.

The gate is deterministic: it plants a known byte sequence, runs
``scanner.scan(...)``, and checks whether any reported finding overlaps the
planted span. No LLM involved.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from codeprobe.snapshot.scanners import Finding, Scanner

# A distinctive, never-otherwise-present-in-real-data string. We deliberately
# embed it as source here — it is NOT a secret, just a sentinel.
CANARY_DEFAULT: str = "CODEPROBE_CANARY_7f3e9b2a8d1c5e4f_test_token"


class CanaryFailedError(RuntimeError):
    """Raised when the scanner fails to detect the planted canary."""


class CanaryProofInvalidError(RuntimeError):
    """Raised when a loaded canary proof fails validation (e.g. passed=False).

    Distinct from :class:`CanaryFailedError` so CLI callers can surface a
    different message for "proof file is malformed / marked as failing"
    versus "scanner actually missed the canary during a live run".
    """


_LEGACY_EXCEPTION_ALIASES = {
    "CanaryFailed": "CanaryFailedError",
    "CanaryProofInvalid": "CanaryProofInvalidError",
}


def __getattr__(name: str) -> object:
    """Legacy-alias shim — see :mod:`codeprobe.calibration.gate` for rationale."""
    new_name = _LEGACY_EXCEPTION_ALIASES.get(name)
    if new_name is not None:
        import warnings

        warnings.warn(
            f"{name} is deprecated; use {new_name}. "
            "The alias will be removed in v0.9.",
            DeprecationWarning,
            stacklevel=2,
        )
        return globals()[new_name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


@dataclass(frozen=True)
class CanaryResult:
    """Outcome of a single canary-gate run.

    Serialized to JSON and persisted alongside the snapshot so downstream
    consumers can independently verify that the gate was exercised.
    """

    passed: bool
    canary: str
    scanner_name: str
    findings: list[Finding]
    timestamp: str

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "canary": self.canary,
            "scanner_name": self.scanner_name,
            "timestamp": self.timestamp,
            "findings": [asdict(f) for f in self.findings],
        }


@dataclass
class CanaryGate:
    """Gate that forces a scanner to prove it catches the planted canary."""

    scanner: Scanner
    canary: str = CANARY_DEFAULT

    def prove(self) -> CanaryResult:
        """Plant the canary and run the scanner against it."""
        blob = (
            b"# planted canary block\n"
            b"password = '" + self.canary.encode("utf-8") + b"'\n"
        )
        findings = self.scanner.scan(blob)
        passed = any(self.canary.encode("utf-8") in blob[f.start : f.end]
                     or self.canary in f.match_preview
                     or _canary_overlaps(f, self.canary, blob)
                     for f in findings)
        # Fallback: if the scanner reports *any* finding at all that covers
        # the planted span, count that as a catch. A scanner with custom rule
        # IDs may not expose the raw secret, so we also accept any finding
        # whose [start,end] overlaps the known canary offset.
        if not passed and findings:
            canary_bytes = self.canary.encode("utf-8")
            idx = blob.find(canary_bytes)
            if idx >= 0:
                canary_end = idx + len(canary_bytes)
                for f in findings:
                    if f.end > idx and f.start < canary_end:
                        passed = True
                        break
        return CanaryResult(
            passed=passed,
            canary=self.canary,
            scanner_name=getattr(self.scanner, "name", "unknown"),
            findings=list(findings),
            timestamp=datetime.now(UTC).isoformat(),
        )

    def require_pass_or_raise(self) -> CanaryResult:
        result = self.prove()
        if not result.passed:
            raise CanaryFailedError(
                f"Canary gate failed: scanner {result.scanner_name!r} did not "
                f"detect the planted canary. Refusing to export source-bearing "
                f"snapshot."
            )
        return result


def _canary_overlaps(finding: Finding, canary: str, blob: bytes) -> bool:
    canary_bytes = canary.encode("utf-8")
    idx = blob.find(canary_bytes)
    if idx < 0:
        return False
    canary_end = idx + len(canary_bytes)
    return finding.end > idx and finding.start < canary_end


def load_canary_proof(path: Path) -> CanaryResult:
    """Load a previously-recorded canary proof from disk.

    The loaded proof is validated eagerly: if ``passed`` is not ``True``,
    :class:`CanaryProofInvalidError` is raised so callers cannot accidentally
    feed a failed proof into :func:`codeprobe.snapshot.redact.redact`. The
    CLI performs its own belt-and-suspenders check on top of this.
    """
    raw = json.loads(Path(path).read_text())
    findings = [
        Finding(
            rule_id=str(f.get("rule_id", "unknown")),
            start=int(f.get("start", 0)),
            end=int(f.get("end", 0)),
            match_preview=str(f.get("match_preview", "")),
            scanner=str(f.get("scanner", raw.get("scanner_name", "unknown"))),
        )
        for f in raw.get("findings", [])
    ]
    result = CanaryResult(
        passed=bool(raw.get("passed", False)),
        canary=str(raw.get("canary", CANARY_DEFAULT)),
        scanner_name=str(raw.get("scanner_name", "unknown")),
        findings=findings,
        timestamp=str(raw.get("timestamp", "")),
    )
    if not result.passed:
        raise CanaryProofInvalidError(
            f"canary proof at {path} has passed=False (scanner="
            f"{result.scanner_name!r}); refusing to load it as a passing "
            f"proof."
        )
    return result
