"""BC-H-04 & BC-M-05: canary gate enforcement and proof validation.

The fixes ensure:

1. ``redact(mode="contents")`` refuses to run unless a passing canary proof
   is supplied OR an inline canary gate succeeds — same protection as
   ``secrets`` mode. Programmatic callers cannot bypass the gate by using
   ``contents`` instead of ``secrets``.
2. :func:`load_canary_proof` raises :class:`CanaryProofInvalid` when the
   loaded proof has ``passed=False``, so callers cannot accidentally pass
   a known-failing proof through.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from codeprobe.snapshot import (
    CANARY_DEFAULT,
    CanaryFailed,
    CanaryProofInvalid,
    CanaryResult,
    MockScanner,
    load_canary_proof,
    redact,
)


def _make_src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello\n")
    return src


def test_contents_mode_without_passing_canary_raises(tmp_path: Path) -> None:
    """A scanner that misses the canary blocks contents-mode redaction."""
    src = _make_src(tmp_path)
    out = tmp_path / "snap"
    # Scanner catches *something* but NOT the planted canary string.
    broken_scanner = MockScanner(hit_substrings=[b"nothing-matches-here"])
    with pytest.raises(CanaryFailed):
        redact(
            source_dir=src,
            mode="contents",
            out_dir=out,
            scanner=broken_scanner,
            allow_source_in_export=True,
        )


def test_contents_mode_rejects_failing_canary_proof(tmp_path: Path) -> None:
    """Supplying a proof with passed=False must be refused."""
    src = _make_src(tmp_path)
    out = tmp_path / "snap"
    failing_proof = CanaryResult(
        passed=False,
        canary=CANARY_DEFAULT,
        scanner_name="mock",
        findings=[],
        timestamp="2026-04-22T00:00:00+00:00",
    )
    with pytest.raises(PermissionError):
        redact(
            source_dir=src,
            mode="contents",
            out_dir=out,
            scanner=MockScanner(hit_substrings=[CANARY_DEFAULT]),
            canary_proof=failing_proof,
            allow_source_in_export=True,
        )


def test_contents_mode_accepts_passing_canary_proof(tmp_path: Path) -> None:
    """A pre-computed passing proof lets contents-mode through."""
    src = _make_src(tmp_path)
    out = tmp_path / "snap"

    passing_proof = CanaryResult(
        passed=True,
        canary=CANARY_DEFAULT,
        scanner_name="mock",
        findings=[],
        timestamp="2026-04-22T00:00:00+00:00",
    )
    # Use a no-op scanner for redaction itself — the gate was already proven
    # by the supplied canary_proof, so the scanner's own detect behaviour is
    # not re-exercised here.
    noop_scanner = MockScanner(hit_substrings=[])
    manifest = redact(
        source_dir=src,
        mode="contents",
        out_dir=out,
        scanner=noop_scanner,
        canary_proof=passing_proof,
        allow_source_in_export=True,
    )
    assert manifest.mode == "contents"
    assert manifest.canary_result is not None
    assert manifest.canary_result["passed"] is True


def test_load_canary_proof_rejects_failed_proof(tmp_path: Path) -> None:
    """``load_canary_proof`` raises CanaryProofInvalid on passed=False."""
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(
        json.dumps(
            {
                "passed": False,
                "canary": CANARY_DEFAULT,
                "scanner_name": "mock",
                "timestamp": "2026-04-22T00:00:00+00:00",
                "findings": [],
            }
        )
    )
    with pytest.raises(CanaryProofInvalid) as exc:
        load_canary_proof(proof_path)
    # The error message must name the offending path so operators can find
    # the file quickly during triage.
    assert str(proof_path) in str(exc.value)


def test_load_canary_proof_accepts_passing_proof(tmp_path: Path) -> None:
    """Sanity check: the happy path still loads cleanly."""
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(
        json.dumps(
            {
                "passed": True,
                "canary": CANARY_DEFAULT,
                "scanner_name": "pattern",
                "timestamp": "2026-04-22T00:00:00+00:00",
                "findings": [],
            }
        )
    )
    result = load_canary_proof(proof_path)
    assert isinstance(result, CanaryResult)
    assert result.passed is True
    assert result.scanner_name == "pattern"


def test_canary_result_replace_preserves_immutability(tmp_path: Path) -> None:
    """CanaryResult is frozen — mutation requires ``dataclasses.replace``."""
    r = CanaryResult(
        passed=True,
        canary=CANARY_DEFAULT,
        scanner_name="mock",
        findings=[],
        timestamp="2026-04-22T00:00:00+00:00",
    )
    r2 = replace(r, passed=False)
    assert r.passed is True
    assert r2.passed is False
