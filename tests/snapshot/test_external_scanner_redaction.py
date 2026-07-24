"""Fail-closed redaction tests for external secret scanners."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from codeprobe.snapshot import (
    CANARY_DEFAULT,
    CanaryProofInvalidError,
    CanaryResult,
    redact,
)
from codeprobe.snapshot.canary import validate_canary_proof
from codeprobe.snapshot.scanners import (
    ExternalScannerLimits,
    Finding,
    GitleaksScanner,
    Scanner,
    ScannerError,
    TrufflehogScanner,
    _completed_external_scan,
    scanner_configuration_fingerprint,
)

RunStub = Callable[..., subprocess.CompletedProcess[bytes]]


def _passing_proof(scanner: Scanner | str) -> CanaryResult:
    effective_scanner = (
        GitleaksScanner()
        if scanner == "gitleaks"
        else TrufflehogScanner()
        if scanner == "trufflehog"
        else scanner
    )
    assert not isinstance(effective_scanner, str)
    finding = _canary_finding(effective_scanner.name)
    return CanaryResult(
        passed=True,
        canary=CANARY_DEFAULT,
        scanner_name=effective_scanner.name,
        findings=[finding],
        timestamp=datetime.now(UTC).isoformat(),
        scanner_fingerprint=scanner_configuration_fingerprint(effective_scanner),
    )


def _canary_finding(scanner_name: str) -> Finding:
    blob = b"# planted canary block\npassword = '" + CANARY_DEFAULT.encode() + b"'\n"
    start = blob.index(CANARY_DEFAULT.encode())
    return Finding(
        rule_id="test-canary",
        start=start,
        end=start + len(CANARY_DEFAULT.encode()),
        match_preview="synthetic-canary",
        scanner=scanner_name,
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
        lambda _binary: sys.executable,
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
        lambda _binary: sys.executable,
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
        lambda _binary: sys.executable,
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
        lambda _binary: sys.executable,
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
        lambda _binary: sys.executable,
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
        lambda _binary: sys.executable,
    )
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.subprocess.run",
        lambda args, **kwargs: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(args, kwargs["timeout"], stderr=b"secret")
        ),
    )

    scanner = GitleaksScanner(
        limits=ExternalScannerLimits(timeout_seconds=0.01)
    )
    with pytest.raises(ScannerError, match="timed out"):
        redact(
            source,
            "contents",
            output,
            scanner=scanner,
            canary_proof=_passing_proof(scanner),
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


def test_external_stdout_is_killed_at_size_limit_during_execution() -> None:
    limits = ExternalScannerLimits(max_output_bytes=4_096)
    with tempfile.TemporaryFile() as output:
        with pytest.raises(ScannerError, match="size limit"):
            _completed_external_scan(
                "test-scanner",
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stdout.buffer.write(b'x' * 1_000_000)",
                ],
                limits=limits,
                stdout=output,
            )
        assert os.fstat(output.fileno()).st_size <= limits.max_output_bytes + 1


def test_external_report_is_killed_at_size_limit_during_execution(
    tmp_path: Path,
) -> None:
    limits = ExternalScannerLimits(max_output_bytes=4_096)
    report = tmp_path / "report.json"

    with pytest.raises(ScannerError, match="size limit"):
        _completed_external_scan(
            "test-scanner",
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; "
                "Path(sys.argv[1]).write_bytes(b'x' * 1_000_000)",
                str(report),
            ],
            limits=limits,
            stdout=subprocess.DEVNULL,
            monitored_path=report,
        )

    assert report.stat().st_size <= limits.max_output_bytes + 1


def test_external_timeout_terminates_scanner_process_group(
    tmp_path: Path,
) -> None:
    child_pid_path = tmp_path / "child.pid"
    program = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(60)"
    )

    with pytest.raises(ScannerError, match="timed out"):
        _completed_external_scan(
            "test-scanner",
            [sys.executable, "-c", program, str(child_pid_path)],
            limits=ExternalScannerLimits(timeout_seconds=0.2),
            stdout=subprocess.DEVNULL,
        )

    child_pid = int(child_pid_path.read_text())
    deadline = time.monotonic() + 2
    while _process_is_running(child_pid) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not _process_is_running(child_pid)


def _process_is_running(pid: int) -> bool:
    try:
        status = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return status.split()[2] != "Z"


def test_canary_proof_rejects_replaced_external_scanner_binary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "gitleaks-first"
    second = tmp_path / "gitleaks-second"
    first.write_bytes(b"first scanner executable")
    second.write_bytes(b"replacement scanner executable")
    scanner = GitleaksScanner()
    active = first
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: str(active),
    )
    proof = CanaryResult(
        passed=True,
        canary=CANARY_DEFAULT,
        scanner_name=scanner.name,
        findings=[_canary_finding(scanner.name)],
        timestamp=datetime.now(UTC).isoformat(),
        scanner_fingerprint=scanner_configuration_fingerprint(scanner),
    )

    active = second

    with pytest.raises(CanaryProofInvalidError, match="configuration"):
        validate_canary_proof(proof, scanner)


def test_canary_proof_rejects_changed_external_scanner_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "gitleaks"
    executable.write_bytes(b"scanner executable")
    config = tmp_path / "gitleaks.toml"
    config.write_text("[allowlist]\n")
    scanner = GitleaksScanner()
    monkeypatch.setattr(
        "codeprobe.snapshot.scanners.shutil.which",
        lambda _binary: str(executable),
    )
    monkeypatch.setenv("GITLEAKS_CONFIG", str(config))
    proof = CanaryResult(
        passed=True,
        canary=CANARY_DEFAULT,
        scanner_name=scanner.name,
        findings=[_canary_finding(scanner.name)],
        timestamp=datetime.now(UTC).isoformat(),
        scanner_fingerprint=scanner_configuration_fingerprint(scanner),
    )

    config.write_text("[allowlist]\ndescription = 'changed'\n")

    with pytest.raises(CanaryProofInvalidError, match="configuration"):
        validate_canary_proof(proof, scanner)
