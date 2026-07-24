"""AC4: hash manifest in SNAPSHOT.json detects single-byte tampering.

Two tamper surfaces:

1. Mutating the manifest itself (one byte of a sha256 field).
2. Mutating a redacted body file under ``files/`` (content-mode snapshot).

Both must cause ``verify_snapshot_extended`` to return ``ok=False``.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path

import pytest

from codeprobe.snapshot import (
    CANARY_DEFAULT,
    MockScanner,
    RedactionMode,
    create_snapshot,
    redact,
    safe_io,
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

    # Scanner that catches the canary (required to clear the gate) but
    # whose redact() is effectively a pass-through on our experiment files
    # because none of them contain the canary substring. This keeps the
    # on-disk redacted bytes equal to the source bytes, which is what this
    # tamper test needs.
    scanner = MockScanner(hit_substrings=[CANARY_DEFAULT])
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


def test_verify_rejects_manifest_body_path_escape(tmp_path: Path) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("outside-secret\n")
    snapshot = tmp_path / "snapshot"
    (snapshot / "files").mkdir(parents=True)
    victim_hash = hashlib.sha256(victim.read_bytes()).hexdigest()
    files = [
        {
            "path": "../../victim.txt",
            "sha256": victim_hash,
            "size": victim.stat().st_size,
            "redacted_body": None,
            "redacted_body_sha256": victim_hash,
        }
    ]
    body = {
        "mode": "contents",
        "source": "untrusted",
        "files": files,
    }
    body_bytes = json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    manifest = {
        **body,
        "attestation": {
            "kind": "unsigned",
            "signature": "",
            "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
            "redaction_mode": "contents",
            "scanner_name": "untrusted",
            "canary": None,
            "timestamp": "2026-07-24T00:00:00+00:00",
        },
    }
    (snapshot / "SNAPSHOT.json").write_text(json.dumps(manifest))

    result = verify_snapshot_extended(snapshot)

    assert result.ok is False
    assert result.file_hashes_match is False
    assert "../../victim.txt" in result.offending_paths


@pytest.mark.parametrize("mode", ["hashes-only", "contents"])
def test_extended_verifier_accepts_base_redaction_snapshot(
    tmp_path: Path,
    mode: RedactionMode,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "result.json").write_text('{"reward": 1.0}\n')
    snapshot = tmp_path / "snapshot"
    if mode == "contents":
        redact(
            source,
            mode,
            snapshot,
            scanner=MockScanner(hit_substrings=[CANARY_DEFAULT]),
            allow_source_in_export=True,
        )
    else:
        redact(source, mode, snapshot)

    result = verify_snapshot_extended(snapshot)

    assert result.ok is True, result.reason


def test_verify_does_not_follow_manifest_symlink(tmp_path: Path) -> None:
    source = _make_experiment(tmp_path)
    external = tmp_path / "external"
    create_snapshot(source, external)
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "SNAPSHOT.json").symlink_to(external / "SNAPSHOT.json")

    result = verify_snapshot_extended(snapshot)

    assert result.ok is False
    assert result.base.ok is False
    assert "manifest" in result.base.reason


def test_verify_rejects_missing_declared_content_body(tmp_path: Path) -> None:
    experiment = _make_experiment(tmp_path)
    snapshot = tmp_path / "snapshot"
    create_snapshot(
        experiment,
        snapshot,
        mode="contents",
        scanner=MockScanner(hit_substrings=[CANARY_DEFAULT]),
        allow_source_in_export=True,
    )
    missing = snapshot / "files" / "baseline" / "task_0001" / "result.json"
    missing.unlink()

    result = verify_snapshot_extended(snapshot)

    assert result.ok is False
    assert result.file_hashes_match is False
    assert str(missing) in result.offending_paths


def test_verify_rejects_tampered_publishable_trace_copy(tmp_path: Path) -> None:
    experiment = _make_experiment(tmp_path)
    snapshot = tmp_path / "snapshot"
    create_snapshot(
        experiment,
        snapshot,
        mode="contents",
        scanner=MockScanner(hit_substrings=[CANARY_DEFAULT]),
        allow_source_in_export=True,
    )
    tampered = (
        snapshot / "export" / "traces" / "baseline" / "task_0001" / "result.json"
    )
    tampered.write_text("tampered\n")

    result = verify_snapshot_extended(snapshot)

    assert result.ok is False
    assert result.file_hashes_match is False
    assert str(tampered) in result.offending_paths


def test_verify_rejects_unmanifested_publishable_trace_body(tmp_path: Path) -> None:
    experiment = _make_experiment(tmp_path)
    snapshot = tmp_path / "snapshot"
    create_snapshot(
        experiment,
        snapshot,
        mode="contents",
        scanner=MockScanner(hit_substrings=[CANARY_DEFAULT]),
        allow_source_in_export=True,
    )
    injected = snapshot / "export" / "traces" / "baseline" / "task_0001" / "extra"
    injected.write_text("not authenticated\n")

    result = verify_snapshot_extended(snapshot)

    assert result.ok is False
    assert result.file_hashes_match is False
    assert str(injected) in result.offending_paths


@pytest.mark.parametrize(
    "relative_parent",
    [
        "files/baseline/task_0001",
        "traces/baseline/task_0001",
        "export/traces/baseline/task_0001",
    ],
)
def test_verify_rejects_unmanifested_fifo(
    tmp_path: Path,
    relative_parent: str,
) -> None:
    experiment = _make_experiment(tmp_path)
    snapshot = tmp_path / "snapshot"
    create_snapshot(
        experiment,
        snapshot,
        mode="contents",
        scanner=MockScanner(hit_substrings=[CANARY_DEFAULT]),
        allow_source_in_export=True,
    )
    injected = snapshot / relative_parent / "extra.fifo"
    os.mkfifo(injected)

    result = verify_snapshot_extended(snapshot)

    assert result.ok is False
    assert result.file_hashes_match is False


def test_verify_rejects_tampered_summary_file(tmp_path: Path) -> None:
    experiment = _make_experiment(tmp_path)
    snapshot = tmp_path / "snapshot"
    create_snapshot(experiment, snapshot)
    summary = snapshot / "summary" / "rewards.json"
    summary.write_text('{"entries": ["tampered"]}\n')

    result = verify_snapshot_extended(snapshot)

    assert result.ok is False
    assert str(summary) in result.offending_paths


def test_verify_rejects_extended_schema_downgrade_with_tampered_layout(
    tmp_path: Path,
) -> None:
    experiment = _make_experiment(tmp_path)
    snapshot = tmp_path / "snapshot"
    key = "extended-downgrade-test-key"
    create_snapshot(experiment, snapshot, signing_key=key)
    manifest_path = snapshot / "SNAPSHOT.json"
    manifest = json.loads(manifest_path.read_text())
    del manifest["schema_version"]
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2))
    summary = snapshot / "summary" / "rewards.json"
    summary.write_text('{"entries": ["tampered"]}\n')
    extra = snapshot / "unexpected.txt"
    extra.write_text("not declared\n")

    result = verify_snapshot_extended(snapshot, signing_key=key)

    assert result.ok is False
    assert result.base.body_sha256_matches is False
    assert str(summary) in result.offending_paths
    assert str(extra) in result.offending_paths


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema_version", "999"),
        ("created_at", "2099-01-01T00:00:00+00:00"),
        ("dependencies", {"mcp_tools": [{"name": "tampered"}]}),
    ],
)
def test_verify_rejects_tampered_extended_attestation_fields(
    tmp_path: Path,
    field: str,
    replacement: object,
) -> None:
    experiment = _make_experiment(tmp_path)
    snapshot = tmp_path / "snapshot"
    key = "extended-fields-test-key"
    create_snapshot(experiment, snapshot, signing_key=key)
    manifest_path = snapshot / "SNAPSHOT.json"
    manifest = json.loads(manifest_path.read_text())
    manifest[field] = replacement
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2))

    result = verify_snapshot_extended(snapshot, signing_key=key)

    assert result.ok is False
    assert result.base.body_sha256_matches is False


def test_verify_rejects_unexpected_top_level_file(tmp_path: Path) -> None:
    experiment = _make_experiment(tmp_path)
    snapshot = tmp_path / "snapshot"
    create_snapshot(experiment, snapshot)
    extra = snapshot / "unexpected.txt"
    extra.write_text("not declared\n")

    result = verify_snapshot_extended(snapshot)

    assert result.ok is False
    assert str(extra) in result.offending_paths


@pytest.mark.parametrize(
    "relative_path",
    [
        "traces/baseline/empty-task",
        "export/traces/baseline/empty-task",
    ],
)
def test_verify_rejects_missing_empty_trial_layout_entry(
    tmp_path: Path,
    relative_path: str,
) -> None:
    experiment = _make_experiment(tmp_path)
    (experiment / "baseline" / "empty-task").mkdir()
    snapshot = tmp_path / "snapshot"
    create_snapshot(experiment, snapshot)
    missing = snapshot / relative_path
    if missing.is_symlink():
        missing.unlink()
    else:
        missing.rmdir()

    result = verify_snapshot_extended(snapshot)

    assert result.ok is False
    assert str(missing) in result.offending_paths


def test_verify_rejects_hashes_only_trace_link_to_wrong_internal_target(
    tmp_path: Path,
) -> None:
    experiment = _make_experiment(tmp_path)
    snapshot = tmp_path / "snapshot"
    create_snapshot(experiment, snapshot)
    link = snapshot / "traces" / "baseline" / "task_0001"
    link.unlink()
    link.symlink_to("../../summary", target_is_directory=True)

    result = verify_snapshot_extended(snapshot)

    assert result.ok is False
    assert result.symlinks_contained is False
    assert str(link) in result.offending_paths


def test_verify_invalid_manifest_schema_returns_structured_failure(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "SNAPSHOT.json").write_text("[]")

    result = verify_snapshot_extended(snapshot)

    assert result.ok is False
    assert result.base.ok is False
    assert "schema" in result.reason


def test_extended_verifier_uses_one_descriptor_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = _make_experiment(tmp_path)
    snapshot = tmp_path / "snapshot"
    create_snapshot(
        experiment,
        snapshot,
        mode="contents",
        scanner=MockScanner(hit_substrings=[CANARY_DEFAULT]),
        allow_source_in_export=True,
    )
    verify_module = importlib.import_module("codeprobe.snapshot.verify")

    def repeated_scan_forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("extended verification must reuse one inventory")

    monkeypatch.setattr(
        verify_module,
        "read_source_files",
        repeated_scan_forbidden,
        raising=False,
    )
    monkeypatch.setattr(
        verify_module,
        "read_regular_file",
        repeated_scan_forbidden,
        raising=False,
    )
    monkeypatch.setattr(Path, "rglob", repeated_scan_forbidden)

    result = verify_snapshot_extended(snapshot)

    assert result.ok is True, result.reason


def test_descriptor_inventory_bounds_captured_manifest_bytes(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    (snapshot / "SNAPSHOT.json").write_bytes(b"12345")

    with pytest.raises(safe_io.SymlinkEscapeError, match="size limit"):
        safe_io.inventory_tree(
            snapshot,
            capture_paths=frozenset({"SNAPSHOT.json"}),
            max_capture_bytes=4,
        )
