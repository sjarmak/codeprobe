"""Deterministic secret scanners used by the snapshot redaction pipeline.

Every scanner exposes the same tiny contract::

    class Scanner(Protocol):
        name: str
        def scan(self, data: bytes) -> list[Finding]: ...
        def redact(self, data: bytes) -> bytes: ...

Builtin implementations:

- :class:`PatternScanner` — regex based, in-process, no external tooling.
- :class:`GitleaksScanner` — shells out to the ``gitleaks`` binary.
- :class:`TrufflehogScanner` — shells out to the ``trufflehog`` binary.
- :class:`MockScanner` — unit-test double; configurable hit substrings.

All logic here is deterministic (regex / subprocess exit codes / substring
checks). No LLM, no keyword-based semantic judgment — this is the redaction
path and ZFC forbids model calls here.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass, replace
from pathlib import Path
from typing import IO, Any, Protocol, runtime_checkable

try:
    import resource
except ImportError:  # pragma: no cover - unavailable on non-POSIX platforms
    resource = None  # type: ignore[assignment]


class ScannerUnavailableError(RuntimeError):
    """Raised when an external scanner binary is not installed on PATH."""


class ScannerError(RuntimeError):
    """Raised when a scanner cannot prove that transformed bytes are safe."""


DEFAULT_EXTERNAL_SCANNER_TIMEOUT_SECONDS = 60.0
DEFAULT_EXTERNAL_SCANNER_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
DEFAULT_EXTERNAL_SCANNER_MAX_LINE_BYTES = 512 * 1024
DEFAULT_EXTERNAL_SCANNER_MAX_FINDINGS = 10_000


@dataclass(frozen=True)
class ExternalScannerLimits:
    """Resource ceilings applied to every external scanner invocation."""

    timeout_seconds: float = DEFAULT_EXTERNAL_SCANNER_TIMEOUT_SECONDS
    max_output_bytes: int = DEFAULT_EXTERNAL_SCANNER_MAX_OUTPUT_BYTES
    max_line_bytes: int = DEFAULT_EXTERNAL_SCANNER_MAX_LINE_BYTES
    max_findings: int = DEFAULT_EXTERNAL_SCANNER_MAX_FINDINGS

    def __post_init__(self) -> None:
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("external scanner timeout must be positive")
        integer_limits = (
            self.max_output_bytes,
            self.max_line_bytes,
            self.max_findings,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in integer_limits
        ):
            raise ValueError("external scanner limits must be positive")


_LEGACY_EXCEPTION_ALIASES = {
    "ScannerUnavailable": "ScannerUnavailableError",
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
class Finding:
    """A single secret-match reported by a scanner.

    ``match_preview`` is a short, already-redacted preview of the offending
    span — callers should never log the raw secret.
    """

    rule_id: str
    start: int
    end: int
    match_preview: str
    scanner: str = "unknown"


# Deterministic patterns. Each entry is (rule_id, compiled_regex).
# These are intentionally conservative — the goal is *pattern* matching, not
# classification. A scanner can be augmented with user-configurable patterns
# through ``PatternScanner(patterns=...)``.
_BASE_PATTERNS: list[tuple[str, str]] = [
    ("aws-access-key", r"AKIA[0-9A-Z]{16}"),
    (
        "aws-secret-key",
        r"(?i)aws(?:.{0,20})?['\"][0-9a-zA-Z/+]{40}['\"]",
    ),
    ("github-token", r"ghp_[A-Za-z0-9]{36,}"),
    ("github-oauth", r"gho_[A-Za-z0-9]{36,}"),
    ("llm-provider-key-sk-ant", r"sk-ant-[A-Za-z0-9\-_]{20,}"),
    ("llm-provider-key-sk", r"sk-[A-Za-z0-9]{20,}"),
    ("slack-token", r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    ("generic-private-key", r"-----BEGIN[ A-Z]*PRIVATE KEY-----"),
    # Generic "password = 'xxxx'" assignment — intentionally narrow to avoid
    # false positives against documentation prose.
    (
        "generic-password-assign",
        r"(?i)(password|passwd|secret|api[_-]?key)\s*[:=]\s*['\"][^'\"\s]{8,}['\"]",
    ),
]


DEFAULT_PATTERNS: list[tuple[str, re.Pattern[bytes]]] = [
    (rule_id, re.compile(pat.encode("utf-8"))) for rule_id, pat in _BASE_PATTERNS
]


@runtime_checkable
class Scanner(Protocol):
    """Protocol every snapshot scanner must satisfy."""

    name: str

    def scan(self, data: bytes) -> list[Finding]:
        """Return findings for ``data``. Empty list means 'looks clean'."""
        ...

    def redact(self, data: bytes) -> bytes:
        """Return ``data`` with all findings overwritten by a redaction marker."""
        ...


@dataclass
class PatternScanner:
    """Regex-based scanner running entirely in-process.

    ``patterns`` accepts a list of ``(rule_id, compiled_regex)`` tuples. Each
    regex MUST be a byte-mode regex (``re.compile(b"...")``); text-mode regex
    raises ``TypeError`` at scan time because bodies are binary.
    """

    name: str = "pattern"
    patterns: list[tuple[str, re.Pattern[bytes]]] = field(
        default_factory=lambda: list(DEFAULT_PATTERNS)
    )

    def scan(self, data: bytes) -> list[Finding]:
        findings: list[Finding] = []
        for rule_id, regex in self.patterns:
            for m in regex.finditer(data):
                preview = _safe_preview(m.group(0))
                findings.append(
                    Finding(
                        rule_id=rule_id,
                        start=m.start(),
                        end=m.end(),
                        match_preview=preview,
                        scanner=self.name,
                    )
                )
        return findings

    def redact(self, data: bytes) -> bytes:
        # Apply all regexes; replacement carries rule_id for traceability.
        out = data
        for rule_id, regex in self.patterns:
            marker = f"[REDACTED:{rule_id}]".encode()
            out = regex.sub(marker, out)
        return out


def _safe_preview(secret: bytes, head: int = 4, tail: int = 2) -> str:
    """Short, redacted preview of a matched secret. Never log the raw value."""
    # ``errors='replace'`` cannot raise — no except clause needed here.
    s = secret.decode("utf-8", errors="replace")
    if len(s) <= head + tail:
        return "*" * len(s)
    return f"{s[:head]}...{s[-tail:]}"


def _redact_findings(data: bytes, findings: list[Finding], scanner_name: str) -> bytes:
    """Replace the union of structurally valid finding spans."""
    spans: list[tuple[int, int]] = []
    for index, finding in enumerate(findings):
        if (
            type(finding.start) is not int
            or type(finding.end) is not int
            or finding.start < 0
            or finding.start >= finding.end
            or finding.end > len(data)
        ):
            raise ScannerError(
                f"{scanner_name} finding {index} has an invalid byte span"
            )
        spans.append((finding.start, finding.end))

    if not spans:
        return data

    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))

    marker = f"[REDACTED:{scanner_name}]".encode()
    redacted_parts: list[bytes] = []
    cursor = 0
    for start, end in merged:
        redacted_parts.extend((data[cursor:start], marker))
        cursor = end
    redacted_parts.append(data[cursor:])
    return b"".join(redacted_parts)


def _line_column_span(
    data: bytes,
    *,
    line_starts: list[int],
    start_line: object,
    end_line: object,
    start_column: object,
    end_column: object,
    finding_index: int,
) -> tuple[int, int]:
    """Convert Gitleaks line/column coordinates to a byte slice."""
    coordinates = (start_line, end_line, start_column, end_column)
    if any(type(value) is not int or value <= 0 for value in coordinates):
        raise ScannerError(
            f"gitleaks finding {finding_index} has malformed coordinates"
        )

    start_line_number = start_line
    end_line_number = end_line
    start_column_number = start_column
    end_column_number = end_column
    assert isinstance(start_line_number, int)
    assert isinstance(end_line_number, int)
    assert isinstance(start_column_number, int)
    assert isinstance(end_column_number, int)
    if (
        start_line_number > len(line_starts)
        or end_line_number > len(line_starts)
        or end_line_number < start_line_number
    ):
        raise ScannerError(
            f"gitleaks finding {finding_index} has out-of-range coordinates"
        )

    # Gitleaks' location calculation retains the preceding newline byte as
    # its origin, so columns after line one carry one additional byte.
    start_origin_offset = 1 if start_line_number > 1 else 0
    end_origin_offset = 1 if end_line_number > 1 else 0
    start = (
        line_starts[start_line_number - 1]
        + start_column_number
        - 1
        - start_origin_offset
    )
    end = (
        line_starts[end_line_number - 1] + end_column_number - end_origin_offset
    )
    start_line_end = (
        line_starts[start_line_number] - 1
        if start_line_number < len(line_starts)
        else len(data)
    )
    end_line_end = (
        line_starts[end_line_number] - 1
        if end_line_number < len(line_starts)
        else len(data)
    )
    if start >= start_line_end or end > end_line_end or start >= end:
        raise ScannerError(
            f"gitleaks finding {finding_index} has out-of-range coordinates"
        )
    return start, end


def _required_text_field(
    entry: dict[str, object],
    field_name: str,
    *,
    scanner_name: str,
    finding_index: int,
) -> str:
    value = entry.get(field_name)
    if not isinstance(value, str) or not value:
        raise ScannerError(
            f"{scanner_name} finding {finding_index} has malformed {field_name}"
        )
    return value


def _encoded_tool_value(
    value: str,
    *,
    scanner_name: str,
    finding_index: int,
) -> bytes:
    try:
        return value.encode()
    except UnicodeEncodeError:
        pass
    raise ScannerError(
        f"{scanner_name} finding {finding_index} has malformed text"
    )


def _load_external_json(payload: bytes, malformed_message: str) -> object:
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError):
        pass
    raise ScannerError(malformed_message)


def _trufflehog_reported_values(
    entry: dict[str, object],
    *,
    finding_index: int,
) -> list[bytes]:
    raw_text = _required_text_field(
        entry,
        "Raw",
        scanner_name="trufflehog",
        finding_index=finding_index,
    )
    values = [
        _encoded_tool_value(
            raw_text,
            scanner_name="trufflehog",
            finding_index=finding_index,
        )
    ]

    secret_parts_value = entry.get("SecretParts")
    if secret_parts_value is not None and not isinstance(secret_parts_value, dict):
        raise ScannerError(
            f"trufflehog finding {finding_index} has malformed SecretParts"
        )
    secret_parts: list[bytes] = []
    if isinstance(secret_parts_value, dict):
        for key, value in secret_parts_value.items():
            if not isinstance(key, str) or not key or not isinstance(value, str):
                raise ScannerError(
                    f"trufflehog finding {finding_index} has malformed SecretParts"
                )
            if value:
                secret_parts.append(
                    _encoded_tool_value(
                        value,
                        scanner_name="trufflehog",
                        finding_index=finding_index,
                    )
                )
    values.extend(secret_parts)

    raw_v2_value = entry.get("RawV2")
    if raw_v2_value is not None and not isinstance(raw_v2_value, str):
        raise ScannerError(f"trufflehog finding {finding_index} has malformed RawV2")
    if isinstance(raw_v2_value, str) and raw_v2_value and not secret_parts:
        values.append(
            _encoded_tool_value(
                raw_v2_value,
                scanner_name="trufflehog",
                finding_index=finding_index,
            )
        )
    return list(dict.fromkeys(values))


def _read_bounded_file(path: Path, max_bytes: int) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    file_fd = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            raise ScannerError("external scanner report is not a regular file")
        source = os.fdopen(file_fd, "rb")
        file_fd = -1
        with source:
            content = source.read(max_bytes + 1)
    finally:
        if file_fd >= 0:
            os.close(file_fd)
    if len(content) > max_bytes:
        raise ScannerError("external scanner output exceeded its size limit")
    return content


def _enforce_line_limit(
    payload: bytes,
    max_line_bytes: int,
    *,
    scanner_name: str,
) -> None:
    line_start = 0
    for offset, byte in enumerate(payload):
        if byte != 10:
            continue
        line_end = (
            offset - 1
            if offset > line_start and payload[offset - 1] == 13
            else offset
        )
        if line_end - line_start > max_line_bytes:
            raise ScannerError(f"{scanner_name} exceeded its line size limit")
        line_start = offset + 1
    if len(payload) - line_start > max_line_bytes:
        raise ScannerError(f"{scanner_name} exceeded its line size limit")


def _completed_external_scan(
    scanner_name: str,
    args: list[str],
    *,
    limits: ExternalScannerLimits,
    stdout: int | IO[Any],
    monitored_path: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    if resource is None:
        raise ScannerError(
            f"{scanner_name} cannot enforce live output limits on this platform"
        )

    output_limit = limits.max_output_bytes + 1

    pid_read_fd, pid_write_fd = os.pipe()

    def configure_child() -> None:
        assert resource is not None
        _, hard_limit = resource.getrlimit(resource.RLIMIT_FSIZE)
        effective_limit = (
            output_limit
            if hard_limit == resource.RLIM_INFINITY
            else min(output_limit, hard_limit)
        )
        resource.setrlimit(
            resource.RLIMIT_FSIZE,
            (effective_limit, hard_limit),
        )
        os.write(pid_write_fd, str(os.getpid()).encode())

    execution_error: str | None = None
    process_group_id: int | None = None
    try:
        try:
            completed = subprocess.run(  # noqa: S603 - resolved scanner executable
                args,
                stdout=stdout,
                stderr=subprocess.DEVNULL,
                timeout=limits.timeout_seconds,
                check=False,
                preexec_fn=configure_child,
                pass_fds=(pid_write_fd,),
                start_new_session=True,
                env=dict(environment) if environment is not None else None,
            )
        except subprocess.TimeoutExpired:
            execution_error = f"{scanner_name} scan timed out"
        except OSError:
            execution_error = f"{scanner_name} scan failed to execute"
    finally:
        os.close(pid_write_fd)
        try:
            raw_pid = os.read(pid_read_fd, 32)
            if raw_pid:
                process_group_id = int(raw_pid)
        finally:
            os.close(pid_read_fd)
    if process_group_id is not None:
        _terminate_process_group(process_group_id)
    if execution_error is not None:
        raise ScannerError(execution_error)

    output_size = 0
    if not isinstance(stdout, int):
        try:
            output_size = os.fstat(stdout.fileno()).st_size
        except (AttributeError, OSError):
            raise ScannerError(
                f"{scanner_name} scan output could not be measured"
            ) from None
    if monitored_path is not None:
        try:
            output_size = max(output_size, monitored_path.stat().st_size)
        except FileNotFoundError:
            pass
        except OSError:
            raise ScannerError(
                f"{scanner_name} scan output could not be measured"
            ) from None
    if output_size > limits.max_output_bytes:
        raise ScannerError(f"{scanner_name} output exceeded its size limit")
    return completed


def _terminate_process_group(process_group_id: int) -> None:
    try:
        os.killpg(process_group_id, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        raise ScannerError("external scanner process group could not be terminated")
    for _ in range(10):
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)
    try:
        os.killpg(process_group_id, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        raise ScannerError("external scanner process group could not be terminated")


@dataclass
class GitleaksScanner:
    """Shells out to the ``gitleaks`` CLI.

    Raises :class:`ScannerUnavailableError` in :meth:`scan` / :meth:`redact` if the
    binary is not on ``PATH``. The canary gate exercises the real binary path;
    tests that cannot depend on gitleaks being installed should use
    :class:`MockScanner` instead.
    """

    name: str = "gitleaks"
    binary: str = "gitleaks"
    limits: ExternalScannerLimits = field(default_factory=ExternalScannerLimits)
    _fallback: PatternScanner = field(default_factory=PatternScanner)
    _pinned_runtime: _PinnedExternalRuntime | None = field(
        default=None,
        repr=False,
        metadata={"fingerprint": False},
    )

    def _require(self) -> str:
        if self._pinned_runtime is not None:
            return self._pinned_runtime.executable
        path = shutil.which(self.binary)
        if path is None:
            raise ScannerUnavailableError(
                f"gitleaks binary {self.binary!r} not found on PATH"
            )
        return path

    def scan(self, data: bytes) -> list[Finding]:
        gitleaks = self._require()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "blob"
            target.write_bytes(data)
            report = Path(td) / "report.json"
            # gitleaks exits non-zero when it finds secrets — this is expected.
            proc = _completed_external_scan(
                self.name,
                [
                    gitleaks,
                    "detect",
                    "--no-git",
                    "--ignore-gitleaks-allow",
                    "-s",
                    str(target),
                    "--report-format",
                    "json",
                    "-r",
                    str(report),
                ],
                limits=self.limits,
                stdout=subprocess.DEVNULL,
                monitored_path=report,
                environment=(
                    self._pinned_runtime.environment
                    if self._pinned_runtime is not None
                    else None
                ),
            )
            if proc.returncode not in (0, 1):
                raise ScannerError("gitleaks scan failed")
            try:
                report_bytes = _read_bounded_file(
                    report,
                    self.limits.max_output_bytes,
                )
            except FileNotFoundError:
                raise ScannerError("gitleaks scan failed without a report")
            except OSError:
                raise ScannerError("gitleaks produced an unreadable report") from None
            _enforce_line_limit(
                report_bytes,
                self.limits.max_line_bytes,
                scanner_name=self.name,
            )
            raw = _load_external_json(
                report_bytes,
                "gitleaks produced a malformed report",
            )
        if not isinstance(raw, list):
            raise ScannerError("gitleaks produced a malformed report")
        if len(raw) > self.limits.max_findings:
            raise ScannerError("gitleaks exceeded its finding limit")
        if proc.returncode == 1 and not raw:
            raise ScannerError("gitleaks scan failed without report findings")
        findings: list[Finding] = []
        line_starts = [0]
        line_starts.extend(
            index + 1 for index, byte in enumerate(data) if byte == 10
        )
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise ScannerError(f"gitleaks finding {index} is malformed")
            start, end = _line_column_span(
                data,
                line_starts=line_starts,
                start_line=entry.get("StartLine"),
                end_line=entry.get("EndLine"),
                start_column=entry.get("StartColumn"),
                end_column=entry.get("EndColumn"),
                finding_index=index,
            )
            rule_id = _required_text_field(
                entry,
                "RuleID",
                scanner_name=self.name,
                finding_index=index,
            )
            match = _required_text_field(
                entry,
                "Match",
                scanner_name=self.name,
                finding_index=index,
            )
            match_bytes = _encoded_tool_value(
                match,
                scanner_name=self.name,
                finding_index=index,
            )
            if data[start:end] != match_bytes:
                raise ScannerError(
                    f"gitleaks finding {index} does not match its byte span"
                )
            findings.append(
                Finding(
                    rule_id=rule_id,
                    start=start,
                    end=end,
                    match_preview=_safe_preview(match_bytes),
                    scanner=self.name,
                )
            )
        return findings

    def redact(self, data: bytes) -> bytes:
        externally_redacted = _redact_findings(data, self.scan(data), self.name)
        return self._fallback.redact(externally_redacted)


@dataclass
class TrufflehogScanner:
    """Shells out to the ``trufflehog`` CLI (filesystem mode)."""

    name: str = "trufflehog"
    binary: str = "trufflehog"
    limits: ExternalScannerLimits = field(default_factory=ExternalScannerLimits)
    _fallback: PatternScanner = field(default_factory=PatternScanner)
    _pinned_runtime: _PinnedExternalRuntime | None = field(
        default=None,
        repr=False,
        metadata={"fingerprint": False},
    )

    def _require(self) -> str:
        if self._pinned_runtime is not None:
            return self._pinned_runtime.executable
        path = shutil.which(self.binary)
        if path is None:
            raise ScannerUnavailableError(
                f"trufflehog binary {self.binary!r} not found on PATH"
            )
        return path

    def scan(self, data: bytes) -> list[Finding]:
        trufflehog = self._require()
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryFile() as stdout:
            target = Path(td) / "blob"
            target.write_bytes(data)
            proc = _completed_external_scan(
                self.name,
                [trufflehog, "filesystem", "--json", str(target)],
                limits=self.limits,
                stdout=stdout,
                environment=(
                    self._pinned_runtime.environment
                    if self._pinned_runtime is not None
                    else None
                ),
            )
            stdout.seek(0)
            output = stdout.read(self.limits.max_output_bytes + 1)
        if proc.returncode != 0:
            raise ScannerError("trufflehog scan failed")
        if len(output) > self.limits.max_output_bytes:
            raise ScannerError("trufflehog output exceeded its size limit")
        findings: list[Finding] = []
        report_count = 0
        for index, line in enumerate(output.splitlines()):
            if len(line) > self.limits.max_line_bytes:
                raise ScannerError("trufflehog output exceeded its line size limit")
            line = line.strip()
            if not line:
                continue
            report_count += 1
            if report_count > self.limits.max_findings:
                raise ScannerError("trufflehog exceeded its finding limit")
            entry = _load_external_json(
                line,
                "trufflehog produced malformed output",
            )
            if not isinstance(entry, dict):
                raise ScannerError(f"trufflehog finding {index} is malformed")
            rule_id = _required_text_field(
                entry,
                "DetectorName",
                scanner_name=self.name,
                finding_index=index,
            )
            for reported_value in _trufflehog_reported_values(
                entry,
                finding_index=index,
            ):
                start = data.find(reported_value)
                if start < 0:
                    raise ScannerError(
                        f"trufflehog finding {index} cannot be mapped to source bytes"
                    )
                while start >= 0:
                    if len(findings) >= self.limits.max_findings:
                        raise ScannerError("trufflehog exceeded its finding limit")
                    findings.append(
                        Finding(
                            rule_id=rule_id,
                            start=start,
                            end=start + len(reported_value),
                            match_preview=_safe_preview(reported_value),
                            scanner=self.name,
                        )
                    )
                    start = data.find(reported_value, start + len(reported_value))
        return findings

    def redact(self, data: bytes) -> bytes:
        externally_redacted = _redact_findings(data, self.scan(data), self.name)
        return self._fallback.redact(externally_redacted)


@dataclass
class MockScanner:
    """Test double.

    ``hit_substrings`` are byte or str substrings that, if present in scanned
    data, will be reported as findings. Callers use this to drive the canary
    gate deterministically without depending on gitleaks/trufflehog being on
    PATH.
    """

    hit_substrings: list[bytes | str] = field(default_factory=list)
    name: str = "mock"

    def _needles(self) -> list[bytes]:
        out: list[bytes] = []
        for needle in self.hit_substrings:
            if isinstance(needle, str):
                out.append(needle.encode("utf-8"))
            else:
                out.append(needle)
        return out

    def scan(self, data: bytes) -> list[Finding]:
        findings: list[Finding] = []
        for needle in self._needles():
            idx = data.find(needle)
            if idx >= 0:
                findings.append(
                    Finding(
                        rule_id="mock-hit",
                        start=idx,
                        end=idx + len(needle),
                        match_preview=_safe_preview(needle),
                        scanner=self.name,
                    )
                )
        return findings

    def redact(self, data: bytes) -> bytes:
        out = data
        for needle in self._needles():
            out = out.replace(needle, b"[REDACTED:mock-hit]")
        return out


def _stable_configuration_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    if isinstance(value, Path):
        return {"path": str(value)}
    if isinstance(value, re.Pattern):
        pattern = value.pattern
        encoded = pattern.hex() if isinstance(pattern, bytes) else pattern
        return {"pattern": encoded, "flags": value.flags}
    if isinstance(value, (list, tuple)):
        return [_stable_configuration_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _stable_configuration_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "class": f"{type(value).__module__}.{type(value).__qualname__}",
            "fields": {
                item.name: _stable_configuration_value(getattr(value, item.name))
                for item in fields(value)
                if item.metadata.get("fingerprint", True)
            },
        }
    raise ScannerError("scanner configuration cannot be fingerprinted safely")


_SCANNER_ARTIFACT_CHUNK_BYTES = 1024 * 1024


def _stream_regular_file(
    source_path: Path,
    destination_path: Path | None = None,
    *,
    destination_mode: int = 0o600,
) -> str:
    """Hash a regular file while optionally copying it in bounded chunks."""
    try:
        source_fd = os.open(
            source_path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
        )
    except OSError:
        raise ScannerError("external scanner identity cannot be read") from None
    destination_fd = -1
    digest = hashlib.sha256()
    try:
        if not stat.S_ISREG(os.fstat(source_fd).st_mode):
            raise ScannerError("external scanner identity is not a regular file")
        if destination_path is not None:
            destination_fd = os.open(
                destination_path,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                destination_mode,
            )
        while True:
            chunk = os.read(source_fd, _SCANNER_ARTIFACT_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
            if destination_fd < 0:
                continue
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written == 0:
                    raise ScannerError(
                        "pinned scanner artifact could not be written"
                    )
                view = view[written:]
    except OSError:
        raise ScannerError("pinned scanner artifact could not be captured") from None
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
        os.close(source_fd)
    return digest.hexdigest()


@dataclass(frozen=True)
class _CapturedExternalRuntime:
    identity: dict[str, object]
    executable: str
    environment: dict[str, str]


@dataclass(frozen=True)
class _PinnedExternalRuntime:
    executable: str
    environment: dict[str, str]
    identity: dict[str, object]


def _capture_external_runtime(
    scanner: Scanner,
    private_root: Path | None = None,
) -> _CapturedExternalRuntime:
    assert isinstance(scanner, (GitleaksScanner, TrufflehogScanner))
    resolved = shutil.which(scanner.binary)
    if resolved is None:
        raise ScannerUnavailableError(
            f"{scanner.name} binary {scanner.binary!r} not found on PATH"
        )
    executable_path = Path(resolved).resolve()
    private_executable = private_root / "scanner" if private_root is not None else None
    executable_sha = _stream_regular_file(
        executable_path,
        private_executable,
        destination_mode=0o700,
    )
    environment = dict(os.environ)
    environment_identity: dict[str, object] = {}
    prefix = f"{scanner.name.upper()}_"
    config_index = 0
    for key, value in sorted(environment.items()):
        if not key.startswith(prefix):
            continue
        item: dict[str, object] = {
            "value_sha256": hashlib.sha256(value.encode()).hexdigest()
        }
        if os.path.isfile(value):
            private_config = (
                private_root / f"config-{config_index}"
                if private_root is not None
                else None
            )
            item["file_sha256"] = _stream_regular_file(
                Path(value).resolve(),
                private_config,
            )
            if private_config is not None:
                environment[key] = str(private_config)
            config_index += 1
        environment_identity[key] = item
    return _CapturedExternalRuntime(
        identity={
            "resolved_path": str(executable_path),
            "sha256": executable_sha,
            "environment": environment_identity,
        },
        executable=str(
            private_executable if private_executable is not None else executable_path
        ),
        environment=environment,
    )


@contextmanager
def pinned_scanner(scanner: Scanner | None) -> Iterator[Scanner | None]:
    """Yield an external scanner bound to immutable private runtime artifacts."""
    if not isinstance(scanner, (GitleaksScanner, TrufflehogScanner)):
        yield scanner
        return

    with tempfile.TemporaryDirectory(prefix=f"codeprobe-{scanner.name}-") as temporary:
        private_root = Path(temporary)
        capture = _capture_external_runtime(scanner, private_root)
        runtime = _PinnedExternalRuntime(
            executable=capture.executable,
            environment=capture.environment,
            identity=capture.identity,
        )
        yield replace(
            scanner,
            _fallback=deepcopy(scanner._fallback),
            _pinned_runtime=runtime,
        )


def _external_runtime_identity(scanner: Scanner) -> dict[str, object] | None:
    if not isinstance(scanner, (GitleaksScanner, TrufflehogScanner)):
        return None
    if scanner._pinned_runtime is not None:
        return scanner._pinned_runtime.identity
    return _capture_external_runtime(scanner).identity


def scanner_configuration_fingerprint(scanner: Scanner) -> str:
    """Return a stable digest binding a canary proof to scanner configuration."""
    explicit = getattr(scanner, "configuration_fingerprint", None)
    if callable(explicit):
        value = explicit()
        if not isinstance(value, str) or not value:
            raise ScannerError("scanner configuration fingerprint is invalid")
        payload: object = {"explicit": value}
    else:
        payload = _stable_configuration_value(scanner)
    runtime_identity = _external_runtime_identity(scanner)
    if runtime_identity is not None:
        payload = {
            "configuration": payload,
            "runtime_identity": runtime_identity,
        }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()
