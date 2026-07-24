"""Fail-closed redaction tests for external secret scanners."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from codeprobe.snapshot import CANARY_DEFAULT, CanaryResult, redact
from codeprobe.snapshot.scanners import (
    GitleaksScanner,
    ScannerError,
    TrufflehogScanner,
)

RunStub = Callable[..., subprocess.CompletedProcess[bytes]]


def _passing_proof(scanner_name: str) -> CanaryResult:
    return CanaryResult(
        passed=True,
        canary=CANARY_DEFAULT,
        scanner_name=scanner_name,
        findings=[],
        timestamp="2026-07-24T00:00:00+00:00",
    )


def _gitleaks_entry(data: bytes, secret: bytes) -> dict[str, object]:
    start = data.index(secret)
    end = start + len(secret)
    line_start = data.rfind(b"\n", 0, start) + 1
    line_number = data.count(b"\n", 0, start) + 1
    gitleaks_column_offset = 1 if line_number > 1 else 0
    return {
        "RuleID": "external-only",
        "StartLine": line_number,
        "EndLine": data.count(b"\n", 0, end) + 1,
        "StartColumn": start - line_start + 1 + gitleaks_column_offset,
        "EndColumn": end - line_start + gitleaks_column_offset,
        "Match": secret.decode(),
        "Secret": secret.decode(),
    }


def _gitleaks_runner(
    secret: bytes,
    scanned_bodies: list[bytes],
    *,
    fail_call: int | None = None,
) -> RunStub:
    def run(
        args: Sequence[str],
        *,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        del capture_output, check
        target = Path(args[args.index("-s") + 1])
        assert target.name == "blob"
        report = Path(args[args.index("-r") + 1])
        data = target.read_bytes()
        scanned_bodies.append(data)
        if fail_call == len(scanned_bodies):
            return subprocess.CompletedProcess(args, 2, b"", b"scanner failed")
        entries = [_gitleaks_entry(data, secret)] if secret in data else []
        report.write_text(json.dumps(entries))
        return subprocess.CompletedProcess(args, 1 if entries else 0, b"", b"")

    return run


def _trufflehog_runner(
    secret: bytes,
    scanned_bodies: list[bytes],
) -> RunStub:
    def run(
        args: Sequence[str],
        *,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        del capture_output, check
        data = Path(args[-1]).read_bytes()
        scanned_bodies.append(data)
        stdout = (
            json.dumps({"DetectorName": "ExternalOnly", "Raw": secret.decode()}).encode()
            + b"\n"
            if secret in data
            else b""
        )
        return subprocess.CompletedProcess(args, 0, stdout, b"")

    return run


def test_gitleaks_only_finding_is_redacted_and_rescanned_before_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_secret = b"external-only-credential"
    pattern_secret = b"ghp_" + b"A" * 36
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.txt").write_bytes(
        b"opaque=" + external_secret + b"\nknown=" + pattern_secret + b"\n"
    )
    scanned_bodies: list[bytes] = []
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/gitleaks",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        _gitleaks_runner(external_secret, scanned_bodies),
    )

    output = tmp_path / "snapshot"
    redact(
        source_dir=source,
        mode="contents",
        out_dir=output,
        scanner=GitleaksScanner(),
        canary_proof=_passing_proof("gitleaks"),
        allow_source_in_export=True,
    )

    exported = (output / "files" / "config.txt").read_bytes()
    assert external_secret not in exported
    assert pattern_secret not in exported
    assert b"[REDACTED:gitleaks]" in exported
    assert b"[REDACTED:github-token]" in exported
    assert scanned_bodies == [
        (source / "config.txt").read_bytes(),
        exported,
    ]


def test_trufflehog_only_finding_is_redacted_and_rescanned_before_export(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_secret = b"external-only-credential"
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.txt").write_bytes(b"opaque=" + external_secret + b"\n")
    scanned_bodies: list[bytes] = []
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/trufflehog",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        _trufflehog_runner(external_secret, scanned_bodies),
    )

    output = tmp_path / "snapshot"
    redact(
        source_dir=source,
        mode="contents",
        out_dir=output,
        scanner=TrufflehogScanner(),
        canary_proof=_passing_proof("trufflehog"),
        allow_source_in_export=True,
    )

    exported = (output / "files" / "config.txt").read_bytes()
    assert external_secret not in exported
    assert b"[REDACTED:trufflehog]" in exported
    assert scanned_bodies == [
        (source / "config.txt").read_bytes(),
        exported,
    ]


def test_trufflehog_redacts_every_reported_secret_part(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    access_key = b"AKIA" + b"ABCDEFGHIJKLMNOP"
    secret_key = b"wJalrXUtnFEMI/K7MDENG/" + b"bPxRfiCYEXAMPLEKEY"
    source = tmp_path / "source"
    source.mkdir()
    (source / "credentials").write_bytes(
        b"aws_access_key_id=" + access_key + b"\naws_secret_access_key=" + secret_key
    )
    scanned_bodies: list[bytes] = []

    def multipart_run(
        args: Sequence[str],
        *,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        del capture_output, check
        data = Path(args[-1]).read_bytes()
        scanned_bodies.append(data)
        if access_key not in data and secret_key not in data:
            return subprocess.CompletedProcess(args, 0, b"", b"")
        finding = {
            "DetectorName": "AWS",
            "Raw": access_key.decode(),
            "RawV2": f"{access_key.decode()}:{secret_key.decode()}",
            "SecretParts": {
                "access_key_id": access_key.decode(),
                "secret_access_key": secret_key.decode(),
            },
        }
        return subprocess.CompletedProcess(
            args, 0, json.dumps(finding).encode() + b"\n", b""
        )

    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/trufflehog",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        multipart_run,
    )

    output = tmp_path / "snapshot"
    redact(
        source_dir=source,
        mode="contents",
        out_dir=output,
        scanner=TrufflehogScanner(),
        canary_proof=_passing_proof("trufflehog"),
        allow_source_in_export=True,
    )

    exported = (output / "files" / "credentials").read_bytes()
    assert access_key not in exported
    assert secret_key not in exported
    assert scanned_bodies == [
        (source / "credentials").read_bytes(),
        exported,
    ]


def test_gitleaks_malformed_report_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def malformed_run(
        args: Sequence[str],
        *,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        del capture_output, check
        Path(args[args.index("-r") + 1]).write_text("{not-json")
        return subprocess.CompletedProcess(args, 1, b"", b"")

    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/gitleaks",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        malformed_run,
    )

    with pytest.raises(ScannerError, match="malformed"):
        GitleaksScanner().scan(b"opaque=value\n")


@pytest.mark.parametrize(
    ("report_body", "return_code"),
    [(None, 0), (None, 1), ("[]", 1)],
)
def test_gitleaks_missing_findings_report_fails_closed(
    report_body: str | None,
    return_code: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_findings_run(
        args: Sequence[str],
        *,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        del capture_output, check
        if report_body is not None:
            Path(args[args.index("-r") + 1]).write_text(report_body)
        return subprocess.CompletedProcess(args, return_code, b"", b"")

    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/gitleaks",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        missing_findings_run,
    )

    with pytest.raises(ScannerError, match="failed"):
        GitleaksScanner().scan(b"opaque=value\n")


@pytest.mark.parametrize(
    "data",
    [
        b"first\r\nopaque=external-only-credential\r\n",
        b"first\nopaque=external-only-credential",
    ],
)
def test_gitleaks_maps_findings_after_first_line(
    data: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_secret = b"external-only-credential"
    scanned_bodies: list[bytes] = []
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/gitleaks",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        _gitleaks_runner(external_secret, scanned_bodies),
    )

    findings = GitleaksScanner().scan(data)

    assert len(findings) == 1
    assert data[findings[0].start : findings[0].end] == external_secret


def test_trufflehog_malformed_output_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/trufflehog",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args, 0, b"{not-json\n", b""
        ),
    )

    with pytest.raises(ScannerError, match="malformed"):
        TrufflehogScanner().scan(b"opaque=value\n")


def test_external_rescan_execution_failure_prevents_file_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_secret = b"external-only-credential"
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.txt").write_bytes(b"opaque=" + external_secret + b"\n")
    scanned_bodies: list[bytes] = []
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/gitleaks",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        _gitleaks_runner(external_secret, scanned_bodies, fail_call=2),
    )

    output = tmp_path / "snapshot"
    with pytest.raises(ScannerError, match="failed"):
        redact(
            source_dir=source,
            mode="contents",
            out_dir=output,
            scanner=GitleaksScanner(),
            canary_proof=_passing_proof("gitleaks"),
            allow_source_in_export=True,
        )

    assert len(scanned_bodies) == 2
    assert not (output / "files" / "config.txt").exists()


def test_external_rescan_findings_prevent_file_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external_secret = b"external-only-credential"
    residual = b"[REDACTED:gitleaks]"
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.txt").write_bytes(b"opaque=" + external_secret + b"\n")
    scanned_bodies: list[bytes] = []

    def persistent_finding_run(
        args: Sequence[str],
        *,
        capture_output: bool,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        del capture_output, check
        target = Path(args[args.index("-s") + 1])
        report = Path(args[args.index("-r") + 1])
        data = target.read_bytes()
        scanned_bodies.append(data)
        detected = external_secret if external_secret in data else residual
        report.write_text(json.dumps([_gitleaks_entry(data, detected)]))
        return subprocess.CompletedProcess(args, 1, b"", b"")

    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/gitleaks",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        persistent_finding_run,
    )

    output = tmp_path / "snapshot"
    with pytest.raises(ScannerError, match="still detected"):
        redact(
            source_dir=source,
            mode="contents",
            out_dir=output,
            scanner=GitleaksScanner(),
            canary_proof=_passing_proof("gitleaks"),
            allow_source_in_export=True,
        )

    assert len(scanned_bodies) == 2
    assert not (output / "files" / "config.txt").exists()
