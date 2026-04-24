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

import json
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


class ScannerUnavailable(RuntimeError):
    """Raised when an external scanner binary is not installed on PATH."""


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


@dataclass
class GitleaksScanner:
    """Shells out to the ``gitleaks`` CLI.

    Raises :class:`ScannerUnavailable` in :meth:`scan` / :meth:`redact` if the
    binary is not on ``PATH``. The canary gate exercises the real binary path;
    tests that cannot depend on gitleaks being installed should use
    :class:`MockScanner` instead.
    """

    name: str = "gitleaks"
    binary: str = "gitleaks"
    _fallback: PatternScanner = field(default_factory=PatternScanner)

    def _require(self) -> str:
        path = shutil.which(self.binary)
        if path is None:
            raise ScannerUnavailable(
                f"gitleaks binary {self.binary!r} not found on PATH"
            )
        return path

    def scan(self, data: bytes) -> list[Finding]:
        gitleaks = self._require()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "blob.bin"
            target.write_bytes(data)
            report = Path(td) / "report.json"
            # gitleaks exits non-zero when it finds secrets — this is expected.
            subprocess.run(  # noqa: S603 - controlled path
                [
                    gitleaks,
                    "detect",
                    "--no-git",
                    "-s",
                    str(target),
                    "--report-format",
                    "json",
                    "-r",
                    str(report),
                ],
                capture_output=True,
                check=False,
            )
            if not report.exists():
                return []
            try:
                raw = json.loads(report.read_text())
            except Exception:  # pragma: no cover - malformed report
                return []
        findings: list[Finding] = []
        for entry in raw or []:
            start = int(entry.get("StartColumn", 0))
            end = int(entry.get("EndColumn", 0))
            findings.append(
                Finding(
                    rule_id=str(entry.get("RuleID", "gitleaks")),
                    start=start,
                    end=end,
                    match_preview=_safe_preview(
                        str(entry.get("Secret", "")).encode("utf-8")
                    ),
                    scanner=self.name,
                )
            )
        return findings

    def redact(self, data: bytes) -> bytes:
        # gitleaks does not rewrite files; delegate redaction to the regex
        # fallback so we always strip *something* recognizable. The canary gate
        # ensures gitleaks actually detects its own planted canary before we
        # ever get here.
        return self._fallback.redact(data)


@dataclass
class TrufflehogScanner:
    """Shells out to the ``trufflehog`` CLI (filesystem mode)."""

    name: str = "trufflehog"
    binary: str = "trufflehog"
    _fallback: PatternScanner = field(default_factory=PatternScanner)

    def _require(self) -> str:
        path = shutil.which(self.binary)
        if path is None:
            raise ScannerUnavailable(
                f"trufflehog binary {self.binary!r} not found on PATH"
            )
        return path

    def scan(self, data: bytes) -> list[Finding]:
        trufflehog = self._require()
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "blob.bin"
            target.write_bytes(data)
            proc = subprocess.run(  # noqa: S603 - controlled path
                [trufflehog, "filesystem", "--json", str(target)],
                capture_output=True,
                check=False,
            )
        findings: list[Finding] = []
        for line in proc.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            findings.append(
                Finding(
                    rule_id=str(entry.get("DetectorName", "trufflehog")),
                    start=0,
                    end=0,
                    match_preview=_safe_preview(
                        str(entry.get("Raw", "")).encode("utf-8")
                    ),
                    scanner=self.name,
                )
            )
        return findings

    def redact(self, data: bytes) -> bytes:
        return self._fallback.redact(data)


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
