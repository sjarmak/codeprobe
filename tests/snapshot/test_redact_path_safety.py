"""Descriptor-level path safety tests for the public redaction pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from codeprobe.snapshot import (
    CANARY_DEFAULT,
    CanaryResult,
    Finding,
    MockScanner,
    SnapshotManifest,
    SymlinkEscapeError,
    redact,
    safe_io,
    write_snapshot,
)
from codeprobe.snapshot.scanners import Scanner, scanner_configuration_fingerprint


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "config.txt").write_text("safe=true\n")
    return source


def _passing_proof(scanner: Scanner | None = None) -> CanaryResult:
    effective_scanner = scanner if scanner is not None else MockScanner()
    blob = b"# planted canary block\npassword = '" + CANARY_DEFAULT.encode() + b"'\n"
    start = blob.index(CANARY_DEFAULT.encode())
    return CanaryResult(
        passed=True,
        canary=CANARY_DEFAULT,
        scanner_name=effective_scanner.name,
        findings=[
            Finding(
                rule_id="test-canary",
                start=start,
                end=start + len(CANARY_DEFAULT.encode()),
                match_preview="synthetic-canary",
                scanner=effective_scanner.name,
            )
        ],
        timestamp=datetime.now(UTC).isoformat(),
        scanner_fingerprint=scanner_configuration_fingerprint(effective_scanner),
    )


def test_redact_rejects_source_file_symlink_before_creating_output(
    tmp_path: Path,
) -> None:
    victim = tmp_path / "outside-secret.txt"
    victim.write_text("outside-secret\n")
    source = tmp_path / "source"
    source.mkdir()
    (source / "escape.txt").symlink_to(victim)
    output = tmp_path / "snapshot"

    with pytest.raises(SymlinkEscapeError):
        redact(source, "hashes-only", output)

    assert not output.exists()


def test_redact_rejects_symlink_in_source_parent_path_before_output(
    tmp_path: Path,
) -> None:
    outside_parent = tmp_path / "outside"
    source = outside_parent / "source"
    source.mkdir(parents=True)
    (source / "secret.txt").write_text("outside-secret\n")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside_parent, target_is_directory=True)
    output = tmp_path / "snapshot"

    with pytest.raises(SymlinkEscapeError):
        redact(linked_parent / "source", "hashes-only", output)

    assert not output.exists()


def test_source_file_swap_to_symlink_is_blocked_by_no_follow_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    source_file = source / "config.txt"
    victim = tmp_path / "outside-secret.txt"
    victim.write_text("outside-secret\n")
    output = tmp_path / "snapshot"
    original_open = safe_io.os.open
    swapped = False

    def swap_before_open(
        path: str | bytes | int,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if (
            path == source_file.name
            and dir_fd is not None
            and flags & safe_io.os.O_NOFOLLOW
            and not flags & safe_io.os.O_DIRECTORY
        ):
            source_file.unlink()
            source_file.symlink_to(victim)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_io.os, "open", swap_before_open)

    with pytest.raises(SymlinkEscapeError):
        redact(source, "hashes-only", output)

    assert swapped is True
    assert not output.exists()


def test_source_file_identity_swap_to_regular_hardlink_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    source_file = source / "config.txt"
    displaced = source / "original.txt"
    victim = tmp_path / "outside-secret.txt"
    victim.write_text("outside-secret\n")
    output = tmp_path / "snapshot"
    original_open = safe_io.os.open
    swapped = False

    def swap_before_open(
        path: str | bytes | int,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if (
            not swapped
            and path == source_file.name
            and dir_fd is not None
            and flags & safe_io.os.O_NOFOLLOW
            and not flags & safe_io.os.O_DIRECTORY
        ):
            source_file.rename(displaced)
            source_file.hardlink_to(victim)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_io.os, "open", swap_before_open)

    with pytest.raises(SymlinkEscapeError, match="changed"):
        redact(source, "hashes-only", output)

    assert swapped is True
    assert not output.exists()


def test_source_directory_identity_swap_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    nested = source / "nested"
    nested.mkdir()
    (nested / "inside.txt").write_text("inside\n")
    displaced = source / "original-nested"
    output = tmp_path / "snapshot"
    original_open = safe_io.os.open
    swapped = False

    def swap_before_open(
        path: str | bytes | int,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if (
            not swapped
            and path == nested.name
            and dir_fd is not None
            and flags & safe_io.os.O_DIRECTORY
        ):
            nested.rename(displaced)
            nested.mkdir()
            (nested / "replacement.txt").write_text("replacement\n")
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_io.os, "open", swap_before_open)

    with pytest.raises(SymlinkEscapeError, match="changed"):
        redact(source, "hashes-only", output)

    assert swapped is True
    assert not output.exists()


@pytest.mark.parametrize("link_parent", [False, True])
def test_redact_rejects_preexisting_output_symlink_without_touching_victim(
    tmp_path: Path,
    link_parent: bool,
) -> None:
    source = _source(tmp_path)
    output = tmp_path / "snapshot"
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    victim = victim_dir / "config.txt"
    victim.write_text("do-not-overwrite\n")

    if link_parent:
        output.mkdir()
        (output / "files").symlink_to(victim_dir, target_is_directory=True)
    else:
        (output / "files").mkdir(parents=True)
        (output / "files" / "config.txt").symlink_to(victim)

    with pytest.raises(SymlinkEscapeError):
        redact(
            source,
            "contents",
            output,
            scanner=MockScanner(),
            canary_proof=_passing_proof(),
            allow_source_in_export=True,
        )

    assert victim.read_text() == "do-not-overwrite\n"


def test_redact_rejects_symlink_in_output_parent_path(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    victim = victim_dir / "SNAPSHOT.json"
    victim.write_text("do-not-overwrite\n")
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(victim_dir, target_is_directory=True)

    with pytest.raises(SymlinkEscapeError):
        redact(source, "hashes-only", linked_parent)

    assert victim.read_text() == "do-not-overwrite\n"


@dataclass
class _OutputSwapScanner:
    """Swap ``files/`` after validation but before staged publication."""

    output: Path
    victim_dir: Path
    name: str = "output-swap"

    def scan(self, _data: bytes) -> list[Finding]:
        return []

    def redact(self, data: bytes) -> bytes:
        self.output.mkdir(exist_ok=True)
        files_dir = self.output / "files"
        if files_dir.exists():
            files_dir.rmdir()
        files_dir.symlink_to(self.victim_dir, target_is_directory=True)
        return data


def test_redact_output_parent_swap_cannot_redirect_file_write(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path)
    output = tmp_path / "snapshot"
    victim_dir = tmp_path / "victim"
    victim_dir.mkdir()
    victim = victim_dir / "config.txt"
    victim.write_text("do-not-overwrite\n")
    scanner = _OutputSwapScanner(output=output, victim_dir=victim_dir)

    with pytest.raises(SymlinkEscapeError):
        redact(
            source,
            "contents",
            output,
            scanner=scanner,
            canary_proof=_passing_proof(scanner),
            allow_source_in_export=True,
        )

    assert victim.read_text() == "do-not-overwrite\n"


def test_output_leaf_swap_is_blocked_by_exclusive_no_follow_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    output = tmp_path / "snapshot"
    victim = tmp_path / "victim.txt"
    victim.write_text("do-not-overwrite\n")
    original_open = safe_io.os.open
    swapped = False

    def swap_before_open(
        path: str | bytes | int,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        if (
            path == "config.txt"
            and flags & safe_io.os.O_EXCL
            and dir_fd is not None
        ):
            (Path(f"/proc/self/fd/{dir_fd}") / "config.txt").symlink_to(victim)
            swapped = True
        return original_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(safe_io.os, "open", swap_before_open)

    with pytest.raises(SymlinkEscapeError):
        redact(
            source,
            "contents",
            output,
            scanner=MockScanner(),
            canary_proof=_passing_proof(),
            allow_source_in_export=True,
        )

    assert swapped is True
    assert victim.read_text() == "do-not-overwrite\n"


def test_output_root_swap_cannot_split_bodies_from_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    output = tmp_path / "snapshot"
    displaced = tmp_path / "displaced-snapshot"
    original_write = safe_io.SecureOutputDirectory.write_bytes
    swapped = False

    def swap_root_after_body(
        directory: safe_io.SecureOutputDirectory,
        relative_path: str,
        data: bytes,
    ) -> Path:
        nonlocal swapped
        result = original_write(directory, relative_path, data)
        if relative_path == "files/config.txt":
            directory.path.rename(displaced)
            directory.path.mkdir()
            swapped = True
        return result

    monkeypatch.setattr(
        safe_io.SecureOutputDirectory,
        "write_bytes",
        swap_root_after_body,
    )

    with pytest.raises(SymlinkEscapeError):
        redact(
            source,
            "contents",
            output,
            scanner=MockScanner(),
            canary_proof=_passing_proof(),
            allow_source_in_export=True,
        )

    assert swapped is True
    assert not (output / "SNAPSHOT.json").exists()
    assert not (displaced / "SNAPSHOT.json").exists()


def test_output_subdirectory_swap_cannot_redirect_later_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    (source / "second.txt").write_text("second=true\n")
    output = tmp_path / "snapshot"
    displaced_files = tmp_path / "displaced-files"
    victim_files = tmp_path / "victim-files"
    victim_files.mkdir()
    original_write = safe_io.SecureOutputDirectory.write_bytes
    swapped = False

    def swap_files_after_first_body(
        directory: safe_io.SecureOutputDirectory,
        relative_path: str,
        data: bytes,
    ) -> Path:
        nonlocal swapped
        result = original_write(directory, relative_path, data)
        if relative_path == "files/config.txt":
            (directory.path / "files").rename(displaced_files)
            victim_files.rename(directory.path / "files")
            swapped = True
        return result

    monkeypatch.setattr(
        safe_io.SecureOutputDirectory,
        "write_bytes",
        swap_files_after_first_body,
    )

    with pytest.raises(SymlinkEscapeError):
        redact(
            source,
            "contents",
            output,
            scanner=MockScanner(),
            canary_proof=_passing_proof(),
            allow_source_in_export=True,
        )

    assert swapped is True
    assert not (output / "files" / "second.txt").exists()
    assert not (output / "SNAPSHOT.json").exists()


def test_output_leaf_replacement_after_write_is_detected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    output = tmp_path / "snapshot"
    victim = tmp_path / "victim.txt"
    victim.write_text("outside-secret\n")
    original_write = safe_io.SecureOutputDirectory.write_bytes
    swapped = False

    def replace_leaf_after_write(
        directory: safe_io.SecureOutputDirectory,
        relative_path: str,
        data: bytes,
    ) -> Path:
        nonlocal swapped
        result = original_write(directory, relative_path, data)
        if relative_path == "files/config.txt":
            (directory.path / relative_path).unlink()
            (directory.path / relative_path).symlink_to(victim)
            swapped = True
        return result

    monkeypatch.setattr(
        safe_io.SecureOutputDirectory,
        "write_bytes",
        replace_leaf_after_write,
    )

    with pytest.raises(SymlinkEscapeError, match="file changed"):
        redact(
            source,
            "contents",
            output,
            scanner=MockScanner(),
            canary_proof=_passing_proof(),
            allow_source_in_export=True,
        )

    assert swapped is True
    assert victim.read_text() == "outside-secret\n"
    assert not (output / "SNAPSHOT.json").exists()


def test_source_capture_size_limit_fails_before_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _source(tmp_path)
    output = tmp_path / "snapshot"
    monkeypatch.setattr(safe_io, "MAX_SOURCE_CAPTURE_BYTES", 4)

    with pytest.raises(SymlinkEscapeError, match="size limit"):
        redact(source, "hashes-only", output)

    assert not output.exists()


def test_secure_output_directory_does_not_retain_one_fd_per_directory(
    tmp_path: Path,
) -> None:
    before = len(os.listdir("/proc/self/fd"))

    with safe_io.SecureOutputDirectory(tmp_path / "out") as output:
        for index in range(200):
            output.write_bytes(f"d{index}/body.txt", b"safe")
        during = len(os.listdir("/proc/self/fd"))
        output.ensure_path_unchanged()

    assert during - before < 16


def test_public_snapshot_writer_cleans_partial_output_after_late_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "snapshot"
    original_write = safe_io.SecureOutputDirectory.write_bytes

    def write_then_fail(
        directory: safe_io.SecureOutputDirectory,
        relative_path: str,
        data: bytes,
    ) -> Path:
        result = original_write(directory, relative_path, data)
        if relative_path == "SNAPSHOT.json":
            raise OSError("injected late failure")
        return result

    monkeypatch.setattr(
        safe_io.SecureOutputDirectory,
        "write_bytes",
        write_then_fail,
    )

    with pytest.raises(OSError, match="injected late failure"):
        write_snapshot(
            SnapshotManifest(mode="hashes-only", source="test"),
            output,
        )

    assert not output.exists()
    assert list(tmp_path.glob(".snapshot.tmp-*")) == []


def test_public_snapshot_writer_returns_published_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = write_snapshot(
        SnapshotManifest(mode="hashes-only", source="test"),
        Path("missing") / ".." / "snapshot",
    )

    assert result == tmp_path / "snapshot" / "SNAPSHOT.json"
    assert result.is_file()
