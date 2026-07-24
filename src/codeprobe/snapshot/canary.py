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

import hmac
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from codeprobe.snapshot.safe_io import SymlinkEscapeError, read_regular_file
from codeprobe.snapshot.scanners import (
    Finding,
    Scanner,
    scanner_configuration_fingerprint,
)

# A distinctive, never-otherwise-present-in-real-data string. We deliberately
# embed it as source here — it is NOT a secret, just a sentinel.
CANARY_DEFAULT: str = "ghp_" + "9Qk2Lm7Np4Rs8Tv1" + "Wx5Yz3Ab6Cd0EfGh2Jk9"
CANARY_PROOF_MAX_AGE = timedelta(hours=24)
CANARY_PROOF_FUTURE_TOLERANCE = timedelta(minutes=5)
CANARY_PROOF_MAX_BYTES = 1024 * 1024


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
    scanner_fingerprint: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "canary": self.canary,
            "scanner_name": self.scanner_name,
            "timestamp": self.timestamp,
            "scanner_fingerprint": self.scanner_fingerprint,
            "findings": [asdict(f) for f in self.findings],
        }


@dataclass
class CanaryGate:
    """Gate that forces a scanner to prove it catches the planted canary."""

    scanner: Scanner
    canary: str = CANARY_DEFAULT

    def prove(self) -> CanaryResult:
        """Plant the canary and run the scanner against it."""
        blob = _canary_blob(self.canary)
        findings = self.scanner.scan(blob)
        result = CanaryResult(
            passed=bool(findings),
            canary=self.canary,
            scanner_name=getattr(self.scanner, "name", "unknown"),
            findings=list(findings),
            timestamp=datetime.now(UTC).isoformat(),
            scanner_fingerprint=scanner_configuration_fingerprint(self.scanner),
        )
        if result.passed:
            _require_detection_evidence(result)
        return result

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


def _canary_blob(canary: str) -> bytes:
    return (
        b"# planted canary block\n"
        b"password = '" + canary.encode("utf-8") + b"'\n"
    )


def _require_detection_evidence(proof: CanaryResult) -> None:
    blob = _canary_blob(proof.canary)
    if not proof.findings:
        raise CanaryProofInvalidError(
            "canary proof has no detection evidence"
        )
    for finding in proof.findings:
        if (
            not isinstance(finding, Finding)
            or not isinstance(finding.rule_id, str)
            or not finding.rule_id
            or not isinstance(finding.scanner, str)
            or finding.scanner != proof.scanner_name
            or type(finding.start) is not int
            or type(finding.end) is not int
            or finding.start < 0
            or finding.end > len(blob)
            or finding.start >= finding.end
        ):
            raise CanaryProofInvalidError(
                "canary proof detection evidence is malformed"
            )
    if not any(
        _canary_overlaps(finding, proof.canary, blob)
        for finding in proof.findings
    ):
        raise CanaryProofInvalidError(
            "canary proof detection evidence does not cover the planted canary"
        )


def validate_canary_proof(
    proof: CanaryResult,
    scanner: Scanner,
    *,
    now: datetime | None = None,
) -> None:
    """Fail closed unless a proof is fresh and bound to this exact scanner."""
    proof_strings = (
        proof.canary,
        proof.scanner_name,
        proof.scanner_fingerprint,
        proof.timestamp,
    )
    if (
        not isinstance(proof.passed, bool)
        or any(not isinstance(value, str) or not value for value in proof_strings)
    ):
        raise CanaryProofInvalidError("canary proof is malformed")
    if not proof.passed:
        raise CanaryProofInvalidError("canary proof is marked passed=False")
    if proof.canary != CANARY_DEFAULT:
        raise CanaryProofInvalidError(
            "canary proof does not cover the currently shipped canary"
        )
    _require_detection_evidence(proof)
    scanner_name = getattr(scanner, "name", "unknown")
    expected_fingerprint = scanner_configuration_fingerprint(scanner)
    if proof.scanner_name != scanner_name or not hmac.compare_digest(
        proof.scanner_fingerprint,
        expected_fingerprint,
    ):
        raise CanaryProofInvalidError(
            "canary proof scanner configuration does not match the active scanner"
        )
    try:
        proved_at = datetime.fromisoformat(proof.timestamp)
    except ValueError:
        raise CanaryProofInvalidError(
            "canary proof timestamp is malformed"
        ) from None
    if proved_at.tzinfo is None:
        raise CanaryProofInvalidError("canary proof timestamp must include a timezone")
    current = now if now is not None else datetime.now(UTC)
    proved_at = proved_at.astimezone(UTC)
    if proved_at > current + CANARY_PROOF_FUTURE_TOLERANCE:
        raise CanaryProofInvalidError("canary proof timestamp is in the future")
    if current - proved_at > CANARY_PROOF_MAX_AGE:
        raise CanaryProofInvalidError(
            "canary proof is stale; run the configured scanner canary gate again"
        )


def load_canary_proof(path: Path) -> CanaryResult:
    """Load a previously-recorded canary proof from disk.

    The loaded proof is validated eagerly: if ``passed`` is not ``True``,
    :class:`CanaryProofInvalidError` is raised so callers cannot accidentally
    feed a failed proof into :func:`codeprobe.snapshot.redact.redact`. The
    CLI performs its own belt-and-suspenders check on top of this.
    """
    proof_path = Path(path)
    try:
        body = read_regular_file(
            proof_path.parent,
            proof_path.name,
            max_bytes=CANARY_PROOF_MAX_BYTES,
        )
    except (FileNotFoundError, OSError, SymlinkEscapeError):
        raise CanaryProofInvalidError(
            f"canary proof at {path} cannot be read securely"
        ) from None
    try:
        raw = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
        raise CanaryProofInvalidError(
            f"canary proof at {path} is malformed"
        ) from None
    if not isinstance(raw, dict) or not isinstance(raw.get("passed"), bool):
        raise CanaryProofInvalidError(
            f"canary proof at {path} is malformed"
        )
    if not raw["passed"]:
        scanner_name = raw.get("scanner_name", "unknown")
        raise CanaryProofInvalidError(
            f"canary proof at {path} has passed=False (scanner="
            f"{scanner_name!r}); refusing to load it as a passing proof."
        )
    try:
        canary = _required_string(raw, "canary")
        scanner_name = _required_string(raw, "scanner_name")
        timestamp = _required_string(raw, "timestamp")
        scanner_fingerprint = _required_string(raw, "scanner_fingerprint")
        raw_findings = raw["findings"]
        if not isinstance(raw_findings, list):
            raise TypeError
        findings = [_parse_finding(value) for value in raw_findings]
    except (KeyError, TypeError):
        raise CanaryProofInvalidError(
            f"canary proof at {path} is malformed"
        ) from None
    result = CanaryResult(
        passed=True,
        canary=canary,
        scanner_name=scanner_name,
        findings=findings,
        timestamp=timestamp,
        scanner_fingerprint=scanner_fingerprint,
    )
    if result.canary != CANARY_DEFAULT:
        raise CanaryProofInvalidError(
            f"canary proof at {path} does not cover the currently shipped canary"
        )
    _require_detection_evidence(result)
    return result


def _required_string(raw: dict[str, object], key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value:
        raise TypeError
    return value


def _parse_finding(value: object) -> Finding:
    if not isinstance(value, dict):
        raise TypeError
    rule_id = _required_string(value, "rule_id")
    match_preview = _required_string(value, "match_preview")
    scanner = _required_string(value, "scanner")
    start = value["start"]
    end = value["end"]
    if (
        not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
    ):
        raise TypeError
    return Finding(
        rule_id=rule_id,
        start=start,
        end=end,
        match_preview=match_preview,
        scanner=scanner,
    )
