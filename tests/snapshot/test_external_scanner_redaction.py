"""Fail-closed redaction tests for external secret scanners."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from codeprobe.snapshot import CANARY_DEFAULT, CanaryResult, redact
from codeprobe.snapshot.scanners import (
    ExternalScannerLimits,
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
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
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
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        data = Path(args[-1]).read_bytes()
        scanned_bodies.append(data)
        output = (
            json.dumps({"DetectorName": "ExternalOnly", "Raw": secret.decode()}).encode()
            + b"\n"
            if secret in data
            else b""
        )
        stdout = kwargs["stdout"]
        assert hasattr(stdout, "write")
        stdout.write(output)  # type: ignore[union-attr]
        return subprocess.CompletedProcess(args, 0)

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
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        data = Path(args[-1]).read_bytes()
        scanned_bodies.append(data)
        if access_key not in data and secret_key not in data:
            return subprocess.CompletedProcess(args, 0)
        finding = {
            "DetectorName": "AWS",
            "Raw": access_key.decode(),
            "RawV2": f"{access_key.decode()}:{secret_key.decode()}",
            "SecretParts": {
                "access_key_id": access_key.decode(),
                "secret_access_key": secret_key.decode(),
            },
        }
        stdout = kwargs["stdout"]
        assert hasattr(stdout, "write")
        stdout.write(json.dumps(finding).encode() + b"\n")  # type: ignore[union-attr]
        return subprocess.CompletedProcess(args, 0)

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
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
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

    with pytest.raises(ScannerError, match="malformed") as exc_info:
        GitleaksScanner().scan(b"opaque=value\n")
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


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
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
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
    def malformed_output(
        args: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        stdout = kwargs["stdout"]
        assert hasattr(stdout, "write")
        stdout.write(b"{not-json\n")  # type: ignore[union-attr]
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/trufflehog",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        malformed_output,
    )

    with pytest.raises(ScannerError, match="malformed") as exc_info:
        TrufflehogScanner().scan(b"opaque=value\n")
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


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
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
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


@pytest.mark.parametrize(
    ("scanner", "binary"),
    [
        (GitleaksScanner, "/test/gitleaks"),
        (TrufflehogScanner, "/test/trufflehog"),
    ],
)
def test_external_scanner_timeout_is_generic_and_fail_closed(
    scanner: type[GitleaksScanner] | type[TrufflehogScanner],
    binary: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    leaked_stderr = b"credential-that-must-not-leak"

    def time_out(args: Sequence[str], **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(
            cmd=args,
            timeout=kwargs["timeout"],
            stderr=leaked_stderr,
        )

    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: binary,
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        time_out,
    )

    with pytest.raises(ScannerError, match="timed out") as exc_info:
        scanner(
            limits=ExternalScannerLimits(timeout_seconds=0.01)
        ).scan(b"opaque=value\n")

    assert leaked_stderr.decode() not in str(exc_info.value)
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_gitleaks_report_size_limit_fails_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def oversized_report(args: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        Path(args[args.index("-r") + 1]).write_bytes(b"x" * 17)
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/gitleaks",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        oversized_report,
    )

    with pytest.raises(ScannerError, match="size limit"):
        GitleaksScanner(
            limits=ExternalScannerLimits(max_output_bytes=16)
        ).scan(b"opaque=value\n")


def test_trufflehog_stdout_and_line_limits_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = b'{"DetectorName":"ExternalOnly","Raw":"value"}\n'

    def write_output(
        args: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        stdout = kwargs["stdout"]
        assert hasattr(stdout, "write")
        stdout.write(output)  # type: ignore[union-attr]
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/trufflehog",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        write_output,
    )

    with pytest.raises(ScannerError, match="size limit"):
        TrufflehogScanner(
            limits=ExternalScannerLimits(max_output_bytes=len(output) - 1)
        ).scan(b"opaque=value\n")
    with pytest.raises(ScannerError, match="line size limit"):
        TrufflehogScanner(
            limits=ExternalScannerLimits(
                max_output_bytes=len(output),
                max_line_bytes=len(output) - 2,
            )
        ).scan(b"opaque=value\n")


@pytest.mark.parametrize("scanner_name", ["gitleaks", "trufflehog"])
def test_external_scanner_finding_count_limit_fails_closed(
    scanner_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = b"external-only-credential"
    data = b"first=" + secret + b"\nsecond=" + secret + b"\n"

    def too_many_findings(
        args: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        if scanner_name == "gitleaks":
            report = Path(args[args.index("-r") + 1])
            report.write_text(
                json.dumps(
                    [
                        _gitleaks_entry(data, secret),
                        _gitleaks_entry(data[data.index(b"\n") + 1 :], secret),
                    ]
                )
            )
            return subprocess.CompletedProcess(args, 1)
        stdout = kwargs["stdout"]
        assert hasattr(stdout, "write")
        line = json.dumps(
            {"DetectorName": "ExternalOnly", "Raw": secret.decode()}
        ).encode()
        stdout.write(line + b"\n" + line + b"\n")  # type: ignore[union-attr]
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: f"/test/{scanner_name}",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        too_many_findings,
    )
    scanner = (
        GitleaksScanner(limits=ExternalScannerLimits(max_findings=1))
        if scanner_name == "gitleaks"
        else TrufflehogScanner(limits=ExternalScannerLimits(max_findings=1))
    )

    with pytest.raises(ScannerError, match="finding limit"):
        scanner.scan(data)


def test_external_timeout_leaves_no_snapshot_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.txt").write_text("opaque=value\n")
    output = tmp_path / "snapshot"

    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/gitleaks",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        lambda args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args, kwargs["timeout"], stderr=b"secret")
        ),
    )

    with pytest.raises(ScannerError, match="timed out"):
        redact(
            source,
            "contents",
            output,
            scanner=GitleaksScanner(
                limits=ExternalScannerLimits(timeout_seconds=0.01)
            ),
            canary_proof=_passing_proof("gitleaks"),
            allow_source_in_export=True,
        )

    assert not output.exists()


@pytest.mark.parametrize("timeout", [float("nan"), float("inf"), float("-inf")])
def test_external_scanner_rejects_non_finite_timeout(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        ExternalScannerLimits(timeout_seconds=timeout)


def test_gitleaks_fifo_report_fails_without_blocking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fifo_report(
        args: Sequence[str],
        **_kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        report = Path(args[args.index("-r") + 1])
        report.unlink(missing_ok=True)
        os.mkfifo(report)
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/gitleaks",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        fifo_report,
    )

    with pytest.raises(ScannerError, match="regular file"):
        GitleaksScanner().scan(b"opaque=value\n")


def test_deep_external_json_is_reported_as_generic_scanner_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deeply_nested = b"[" * 2_000 + b"]" * 2_000 + b"\n"

    def deep_output(
        args: Sequence[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        stdout = kwargs["stdout"]
        assert hasattr(stdout, "write")
        stdout.write(deeply_nested)  # type: ignore[union-attr]
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: "/test/trufflehog",
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        deep_output,
    )

    with pytest.raises(ScannerError, match="malformed") as exc_info:
        TrufflehogScanner().scan(b"opaque=value\n")
    assert exc_info.value.__context__ is None
