"""AC4: hash manifest in SNAPSHOT.json detects single-byte tampering.

Two tamper surfaces:

1. Mutating the manifest itself (one byte of a sha256 field).
2. Mutating a redacted body file under ``files/`` (content-mode snapshot).

Both must cause ``verify_snapshot_extended`` to return ``ok=False``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from codeprobe.snapshot import (
    MockScanner,
    create_snapshot,
    verify_snapshot_extended,
)


def _make_experiment(tmp_path: Path) -> Path:
    exp = tmp_path / "experiment"
    trial = exp / "baseline" / "task_0001"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text('{"reward": 1.0}\n')
    (trial / "task_metrics.json").write_text('{"duration": 12.5}\n')
    return exp


def test_manifest_single_byte_tamper_is_detected(tmp_path: Path) -> None:
    """Flip one byte in SNAPSHOT.json's first sha256 — verify must fail."""
    exp = _make_experiment(tmp_path)
    out = tmp_path / "snap"
    key = "tamper-test-key-0001"
    os.environ["CODEPROBE_SIGNING_KEY"] = key
    try:
        create_snapshot(exp, out, signing_key=key)
    finally:
        os.environ.pop("CODEPROBE_SIGNING_KEY", None)

    # Pre-tamper: verify passes.
    before = verify_snapshot_extended(out, signing_key=key)
    assert before.ok is True, before.reason

    # Tamper: flip a single hex char in the first file's sha256.
    manifest_path = out / "SNAPSHOT.json"
    manifest = json.loads(manifest_path.read_text())
    original = manifest["files"][0]["sha256"]
    flipped = ("0" if original[0] != "0" else "1") + original[1:]
    assert flipped != original
    manifest["files"][0]["sha256"] = flipped
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2))

    after = verify_snapshot_extended(out, signing_key=key)
    assert after.ok is False
    assert after.base.body_sha256_matches is False


def test_redacted_body_single_byte_tamper_is_detected(tmp_path: Path) -> None:
    """Flip one byte in a redacted body under files/ — verify must fail.

    We use a ``MockScanner`` with no hit_substrings so redaction is a byte-
    for-byte passthrough; that way the hash in the manifest matches the
    body on disk, and a single-byte post-write flip is detectable.
    """
    exp = _make_experiment(tmp_path)
    out = tmp_path / "snap"

    # Passthrough scanner: redact() leaves bytes intact.
    scanner = MockScanner(hit_substrings=[])
    os.environ.pop("CODEPROBE_SIGNING_KEY", None)
    create_snapshot(
        exp,
        out,
        mode="contents",
        scanner=scanner,
        allow_source_in_export=True,
    )

    # Locate a redacted body on disk.
    files_dir = out / "files"
    assert files_dir.is_dir(), "contents-mode snapshot must materialise files/"

    # Pre-tamper: verify passes for body hashes.
    before = verify_snapshot_extended(out)
    assert before.file_hashes_match is True, before.offending_paths

    # Tamper: flip one byte of a body on disk.
    victim: Path | None = None
    for p in files_dir.rglob("*"):
        if p.is_file():
            victim = p
            break
    assert victim is not None, "expected at least one body under files/"
    data = bytearray(victim.read_bytes())
    data[0] ^= 0x01
    victim.write_bytes(bytes(data))

    after = verify_snapshot_extended(out)
    assert after.ok is False
    assert after.file_hashes_match is False
    assert str(victim) in after.offending_paths


def test_untampered_snapshot_verifies_clean(tmp_path: Path) -> None:
    """Positive control: untouched snapshot returns ok=True everywhere."""
    exp = _make_experiment(tmp_path)
    out = tmp_path / "snap"
    os.environ.pop("CODEPROBE_SIGNING_KEY", None)
    create_snapshot(exp, out)

    result = verify_snapshot_extended(out)
    assert result.file_hashes_match is True
    assert result.symlinks_contained is True
    assert result.base.body_sha256_matches is True
