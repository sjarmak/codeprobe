"""AC6: SNAPSHOT.json carries a signed attestation naming the redaction mode.

Scenarios:

- CODEPROBE_SIGNING_KEY set → attestation.kind == "hmac-sha256",
  signature verifies, redaction_mode is recorded.
- No key → attestation.kind == "unsigned" but body_sha256 is still present.
- Tamper with the manifest → verify_snapshot rejects it.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main as cli_main
from codeprobe.snapshot import (
    PUBLISHABLE_DEFAULT,
    redact,
    verify_snapshot,
)


def _create_src(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("alpha\n")
    (src / "b.txt").write_text("beta\n")
    return src


def test_attestation_present_in_hashes_only_default(tmp_path: Path) -> None:
    src = _create_src(tmp_path)
    out = tmp_path / "snap"
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        ["snapshot", "create", str(src), "--out", str(out)],
    )
    assert result.exit_code == 0, result.output

    manifest = json.loads((out / "SNAPSHOT.json").read_text())
    att = manifest["attestation"]
    assert att["redaction_mode"] == "hashes-only"
    assert att["redaction_mode"] == PUBLISHABLE_DEFAULT
    assert "body_sha256" in att
    assert len(att["body_sha256"]) == 64
    # Mode name must be explicit in the attestation (AC6).
    assert att["kind"] in ("hmac-sha256", "unsigned")


def test_hmac_signed_attestation_verifies(
    tmp_path: Path, monkeypatch: object
) -> None:
    src = _create_src(tmp_path)
    out = tmp_path / "snap"
    key = "super-secret-test-key-0123456789"

    # Use the library directly to control signing key deterministically.
    manifest = redact(
        source_dir=src,
        mode="hashes-only",
        out_dir=out,
        signing_key=key,
    )
    assert manifest.attestation is not None
    assert manifest.attestation.kind == "hmac-sha256"
    assert manifest.attestation.signature != ""

    # Recompute the signature by hand and compare.
    raw = json.loads((out / "SNAPSHOT.json").read_text())
    body = {
        "mode": raw["mode"],
        "source": raw["source"],
        "files": raw["files"],
    }
    if "canary_result" in raw:
        body["canary_result"] = raw["canary_result"]
    serialized = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    expected = hmac.new(key.encode(), serialized, hashlib.sha256).hexdigest()
    assert expected == raw["attestation"]["signature"]
    assert (
        hashlib.sha256(serialized).hexdigest() == raw["attestation"]["body_sha256"]
    )

    result = verify_snapshot(out, signing_key=key)
    assert result.ok is True
    assert result.body_sha256_matches is True
    assert result.signature_matches is True


def test_unsigned_attestation_when_no_key(
    tmp_path: Path, monkeypatch: object
) -> None:
    # Ensure env var unset.
    import os

    os.environ.pop("CODEPROBE_SIGNING_KEY", None)

    src = _create_src(tmp_path)
    out = tmp_path / "snap"
    manifest = redact(
        source_dir=src,
        mode="hashes-only",
        out_dir=out,
        signing_key=None,
    )
    assert manifest.attestation is not None
    assert manifest.attestation.kind == "unsigned"
    assert manifest.attestation.signature == ""
    assert len(manifest.attestation.body_sha256) == 64

    result = verify_snapshot(out, signing_key=None)
    assert result.ok is True
    assert result.body_sha256_matches is True
    assert result.signature_matches is None  # unsigned


def test_tampered_manifest_fails_verification(tmp_path: Path) -> None:
    src = _create_src(tmp_path)
    out = tmp_path / "snap"
    key = "tamper-test-key"
    redact(
        source_dir=src,
        mode="hashes-only",
        out_dir=out,
        signing_key=key,
    )

    # Tamper: flip a file's sha256.
    path = out / "SNAPSHOT.json"
    manifest = json.loads(path.read_text())
    manifest["files"][0]["sha256"] = "0" * 64
    path.write_text(json.dumps(manifest, sort_keys=True, indent=2))

    result = verify_snapshot(out, signing_key=key)
    assert result.ok is False
    assert result.body_sha256_matches is False


def test_attestation_records_mode_for_every_supported_mode(
    tmp_path: Path,
) -> None:
    """Both hashes-only and contents modes must name their mode in the
    attestation block."""
    from codeprobe.snapshot import MockScanner

    # hashes-only via library call.
    src = _create_src(tmp_path)
    out1 = tmp_path / "snap_hashes"
    m1 = redact(src, "hashes-only", out1)
    assert m1.attestation is not None
    assert m1.attestation.redaction_mode == "hashes-only"

    # contents via library (allow_source_in_export must be True).
    out2 = tmp_path / "snap_contents"
    scanner = MockScanner(hit_substrings=["alpha"])
    m2 = redact(
        src,
        "contents",
        out2,
        scanner=scanner,
        allow_source_in_export=True,
    )
    assert m2.attestation is not None
    assert m2.attestation.redaction_mode == "contents"
    assert m2.attestation.scanner_name == "mock"
