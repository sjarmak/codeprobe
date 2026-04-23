"""BC-H-03: verify_snapshot_extended must trust ``redacted_body_sha256``.

Before this fix, verification compared the on-disk (redacted) body against
the manifest's source ``sha256``. For any non-trivial scanner that actually
rewrites bytes, this produced a false "tampered" signal on every file.

The fix writes a per-file ``redacted_body_sha256`` at redaction time and
has the verifier compare against that field. The legacy fallback path is
exercised by the existing tamper-detection tests (which use a passthrough
scanner).
"""

from __future__ import annotations

from pathlib import Path

from codeprobe.snapshot import (
    CANARY_DEFAULT,
    MockScanner,
    create_snapshot,
    verify_snapshot_extended,
)

_SECRET = b"AKIAABCDEFGHIJKL1234"  # Shape matches the aws-access-key pattern.  # gitleaks:allow


def _make_experiment_with_secret(tmp_path: Path) -> Path:
    exp = tmp_path / "experiment"
    trial = exp / "baseline" / "task_0001"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text(
        '{"reward": 1.0, "logline": "access_key=' + _SECRET.decode() + '"}\n'
    )
    (trial / "task_metrics.json").write_text('{"duration": 12.5}\n')
    return exp


def test_verify_passes_on_actually_redacted_snapshot(tmp_path: Path) -> None:
    """A scanner that rewrites bytes must not produce false tamper flags."""
    exp = _make_experiment_with_secret(tmp_path)
    out = tmp_path / "snap"

    # Scanner actively rewrites the planted secret AND catches the canary.
    active_scanner = MockScanner(hit_substrings=[CANARY_DEFAULT, _SECRET])
    create_snapshot(
        exp,
        out,
        mode="contents",
        scanner=active_scanner,
        allow_source_in_export=True,
    )

    # The manifest's file entries must record a redacted_body_sha256 field
    # — that is what verification now compares against.
    import json as _json

    manifest = _json.loads((out / "SNAPSHOT.json").read_text())
    files = manifest["files"]
    assert files, "expected at least one file in the manifest"
    assert any(
        f.get("redacted_body_sha256") for f in files
    ), "redact() must record redacted_body_sha256 for content-mode bodies"

    result = verify_snapshot_extended(out)
    assert result.ok is True, result.reason
    assert result.file_hashes_match is True
    assert result.offending_paths == []


def test_verify_detects_tamper_on_redacted_body(tmp_path: Path) -> None:
    """Flipping a byte in a *really* redacted body must fail verification."""
    exp = _make_experiment_with_secret(tmp_path)
    out = tmp_path / "snap"

    active_scanner = MockScanner(hit_substrings=[CANARY_DEFAULT, _SECRET])
    create_snapshot(
        exp,
        out,
        mode="contents",
        scanner=active_scanner,
        allow_source_in_export=True,
    )

    files_dir = out / "files"
    victim: Path | None = None
    for p in files_dir.rglob("*"):
        if p.is_file():
            victim = p
            break
    assert victim is not None

    # Tamper: flip the first byte.
    data = bytearray(victim.read_bytes())
    data[0] ^= 0x5A
    victim.write_bytes(bytes(data))

    result = verify_snapshot_extended(out)
    assert result.ok is False
    assert result.file_hashes_match is False
    assert str(victim) in result.offending_paths


def test_legacy_snapshot_without_redacted_hash_falls_back(tmp_path: Path) -> None:
    """If a manifest lacks redacted_body_sha256, the verifier falls back to
    the source sha. A passthrough-scanner snapshot (bytes unchanged) must
    still verify cleanly."""
    exp = _make_experiment_with_secret(tmp_path)
    out = tmp_path / "snap"

    # Passthrough scanner — byte-for-byte copy into files/.
    passthrough = MockScanner(hit_substrings=[CANARY_DEFAULT])
    create_snapshot(
        exp,
        out,
        mode="contents",
        scanner=passthrough,
        allow_source_in_export=True,
    )

    # Simulate a legacy manifest: strip redacted_body_sha256 from every
    # file entry. The verifier must fall back to source-sha comparison.
    import json as _json

    manifest_path = out / "SNAPSHOT.json"
    manifest = _json.loads(manifest_path.read_text())
    for f in manifest["files"]:
        f.pop("redacted_body_sha256", None)
    manifest_path.write_text(
        _json.dumps(manifest, sort_keys=True, indent=2)
    )

    # Because the scanner is passthrough, on-disk body hash == source hash,
    # so the fallback path still verifies successfully.
    #
    # Note: the attestation's body_sha256 is computed over the manifest
    # body excluding redacted_body_sha256 historically, so we don't check
    # attestation-level pass/fail here — only the file-hash component.
    result = verify_snapshot_extended(out)
    assert result.file_hashes_match is True
    assert all("files/" not in p for p in result.offending_paths)
