"""BC-H-04 & BC-M-05: canary gate enforcement and proof validation.

The fixes ensure:

1. ``redact(mode="contents")`` refuses to run unless a passing canary proof
   is supplied OR an inline canary gate succeeds — same protection as
   ``secrets`` mode. Programmatic callers cannot bypass the gate by using
   ``contents`` instead of ``secrets``.
2. :func:`load_canary_proof` raises :class:`CanaryProofInvalidError` when the
   loaded proof has ``passed=False``, so callers cannot accidentally pass
   a known-failing proof through.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codeprobe.snapshot import (
    CANARY_DEFAULT,
    CanaryFailedError,
    CanaryGate,
    CanaryProofInvalidError,
    CanaryResult,
    Finding,
    MockScanner,
    PatternScanner,
    load_canary_proof,
    redact,
)


class _MalformedCanaryScanner:
    name = "malformed-canary"

    def configuration_fingerprint(self) -> str:
        return "malformed-canary-test"

    def scan(self, data: bytes) -> list[Finding]:
        return [
            Finding(
                rule_id="malformed",
                start=-1,
                end=len(data) + 100,
                match_preview=CANARY_DEFAULT,
                scanner=self.name,
            )
        ]

    def redact(self, data: bytes) -> bytes:
        return data


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
    with pytest.raises(CanaryFailedError):
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
    with pytest.raises(CanaryProofInvalidError):
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

    scanner = MockScanner(hit_substrings=[CANARY_DEFAULT])
    passing_proof = CanaryGate(scanner).require_pass_or_raise()
    manifest = redact(
        source_dir=src,
        mode="contents",
        out_dir=out,
        scanner=scanner,
        canary_proof=passing_proof,
        allow_source_in_export=True,
    )
    assert manifest.mode == "contents"
    assert manifest.canary_result is not None
    assert manifest.canary_result["passed"] is True


def test_load_canary_proof_rejects_failed_proof(tmp_path: Path) -> None:
    """``load_canary_proof`` raises CanaryProofInvalidError on passed=False."""
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
    with pytest.raises(CanaryProofInvalidError) as exc:
        load_canary_proof(proof_path)
    # The error message must name the offending path so operators can find
    # the file quickly during triage.
    assert str(proof_path) in str(exc.value)


def test_load_canary_proof_accepts_passing_proof(tmp_path: Path) -> None:
    """Sanity check: the happy path still loads cleanly."""
    proof_path = tmp_path / "proof.json"
    proof = CanaryGate(PatternScanner()).require_pass_or_raise()
    proof_path.write_text(json.dumps(proof.to_dict()))
    result = load_canary_proof(proof_path)
    assert isinstance(result, CanaryResult)
    assert result.passed is True
    assert result.scanner_name == "pattern"


def test_load_canary_proof_rejects_success_without_detection_evidence(
    tmp_path: Path,
) -> None:
    proof_path = tmp_path / "proof.json"
    proof = replace(
        CanaryGate(PatternScanner()).require_pass_or_raise(),
        findings=[],
    )
    proof_path.write_text(json.dumps(proof.to_dict()))

    with pytest.raises(CanaryProofInvalidError, match="evidence"):
        load_canary_proof(proof_path)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"passed": "true"},
        {"passed": True, "findings": "not-a-list"},
        {
            "passed": True,
            "canary": CANARY_DEFAULT,
            "scanner_name": "pattern",
            "scanner_fingerprint": "fingerprint",
            "timestamp": "2026-04-22T00:00:00+00:00",
            "findings": [{"start": "0", "end": 1}],
        },
    ],
)
def test_load_canary_proof_rejects_malformed_schema(
    tmp_path: Path,
    payload: object,
) -> None:
    proof_path = tmp_path / "proof.json"
    proof_path.write_text(json.dumps(payload))

    with pytest.raises(CanaryProofInvalidError, match="malformed"):
        load_canary_proof(proof_path)


def test_load_canary_proof_rejects_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text(
        json.dumps(CanaryGate(PatternScanner()).require_pass_or_raise().to_dict())
    )
    proof_path = tmp_path / "proof.json"
    proof_path.symlink_to(target.name)

    with pytest.raises(CanaryProofInvalidError, match="securely"):
        load_canary_proof(proof_path)


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


def test_passing_proof_from_different_scanner_configuration_is_rejected(
    tmp_path: Path,
) -> None:
    src = _make_src(tmp_path)
    proof = CanaryGate(
        MockScanner(hit_substrings=[CANARY_DEFAULT])
    ).require_pass_or_raise()

    with pytest.raises(CanaryProofInvalidError, match="configuration"):
        redact(
            source_dir=src,
            mode="contents",
            out_dir=tmp_path / "snap",
            scanner=MockScanner(hit_substrings=[CANARY_DEFAULT, b"other"]),
            canary_proof=proof,
            allow_source_in_export=True,
        )


def test_programmatic_proof_rejects_success_without_detection_evidence(
    tmp_path: Path,
) -> None:
    src = _make_src(tmp_path)
    scanner = MockScanner(hit_substrings=[CANARY_DEFAULT])
    proof = replace(
        CanaryGate(scanner).require_pass_or_raise(),
        findings=[],
    )

    with pytest.raises(CanaryProofInvalidError, match="evidence"):
        redact(
            source_dir=src,
            mode="contents",
            out_dir=tmp_path / "snap",
            scanner=scanner,
            canary_proof=proof,
            allow_source_in_export=True,
        )


def test_inline_canary_gate_rejects_malformed_detection_evidence(
    tmp_path: Path,
) -> None:
    scanner = _MalformedCanaryScanner()

    with pytest.raises(CanaryProofInvalidError, match="evidence"):
        CanaryGate(scanner).require_pass_or_raise()

    output = tmp_path / "snap"
    with pytest.raises(CanaryProofInvalidError, match="evidence"):
        redact(
            source_dir=_make_src(tmp_path),
            mode="contents",
            out_dir=output,
            scanner=scanner,
            allow_source_in_export=True,
        )
    assert not output.exists()


def test_stale_passing_proof_is_rejected_before_output(tmp_path: Path) -> None:
    src = _make_src(tmp_path)
    scanner = MockScanner(hit_substrings=[CANARY_DEFAULT])
    proof = CanaryGate(scanner).require_pass_or_raise()
    stale = replace(
        proof,
        timestamp=(datetime.now(UTC) - timedelta(days=2)).isoformat(),
    )
    output = tmp_path / "snap"

    with pytest.raises(CanaryProofInvalidError, match="stale"):
        redact(
            source_dir=src,
            mode="contents",
            out_dir=output,
            scanner=scanner,
            canary_proof=stale,
            allow_source_in_export=True,
        )

    assert not output.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("passed", "true"),
        ("canary", 1),
        ("scanner_name", 1),
        ("scanner_fingerprint", 1),
        ("timestamp", 1),
    ],
)
def test_programmatic_canary_proof_rejects_malformed_fields(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    src = _make_src(tmp_path)
    scanner = MockScanner(hit_substrings=[CANARY_DEFAULT])
    proof = replace(
        CanaryGate(scanner).require_pass_or_raise(),
        **{field: value},
    )

    with pytest.raises(CanaryProofInvalidError, match="malformed"):
        redact(
            source_dir=src,
            mode="contents",
            out_dir=tmp_path / "snap",
            scanner=scanner,
            canary_proof=proof,
            allow_source_in_export=True,
        )
