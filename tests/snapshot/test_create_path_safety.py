"""Race and symlink safety for the full CSB snapshot creation path."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from codeprobe.snapshot import (
    CANARY_DEFAULT,
    MockScanner,
    RedactionMode,
    SnapshotManifest,
    SymlinkEscapeError,
    build_extended_manifest,
    create_snapshot,
    safe_io,
    write_extended_manifest,
)
from codeprobe.snapshot import create as create_module


def _experiment(tmp_path: Path) -> Path:
    experiment = tmp_path / "experiment"
    trial = experiment / "baseline" / "task-1"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text('{"ok": true}\n')
    return experiment


def test_create_rejects_output_root_symlink_without_touching_victim(
    tmp_path: Path,
) -> None:
    experiment = _experiment(tmp_path)
    victim = tmp_path / "victim"
    victim.mkdir()
    marker = victim / "SNAPSHOT.json"
    marker.write_text("do-not-overwrite\n")
    output = tmp_path / "snapshot"
    output.symlink_to(victim, target_is_directory=True)

    with pytest.raises(SymlinkEscapeError):
        create_snapshot(experiment, output)

    assert marker.read_text() == "do-not-overwrite\n"


def test_create_rejects_manifest_leaf_symlink_without_touching_victim(
    tmp_path: Path,
) -> None:
    experiment = _experiment(tmp_path)
    output = tmp_path / "snapshot"
    output.mkdir()
    victim = tmp_path / "victim.json"
    victim.write_text("do-not-overwrite\n")
    (output / "SNAPSHOT.json").symlink_to(victim)

    with pytest.raises(SymlinkEscapeError):
        create_snapshot(experiment, output)

    assert victim.read_text() == "do-not-overwrite\n"


def test_public_extended_manifest_writer_rejects_leaf_symlink(
    tmp_path: Path,
) -> None:
    output = tmp_path / "snapshot"
    output.mkdir()
    victim = tmp_path / "victim.json"
    victim.write_text("do-not-overwrite\n")
    (output / "SNAPSHOT.json").symlink_to(victim)
    extended = build_extended_manifest(
        SnapshotManifest(mode="hashes-only", source="test")
    )

    with pytest.raises(SymlinkEscapeError):
        write_extended_manifest(extended, output)

    assert victim.read_text() == "do-not-overwrite\n"


def test_public_extended_manifest_writer_cleans_partial_output_after_late_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "snapshot"
    extended = build_extended_manifest(
        SnapshotManifest(mode="hashes-only", source="test")
    )
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
        write_extended_manifest(extended, output)

    assert not output.exists()
    assert list(tmp_path.glob(".snapshot.tmp-*")) == []


def test_public_extended_manifest_writer_returns_published_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    extended = build_extended_manifest(
        SnapshotManifest(mode="hashes-only", source="test")
    )

    result = write_extended_manifest(
        extended,
        Path("missing") / ".." / "snapshot",
    )

    assert result == tmp_path / "snapshot" / "SNAPSHOT.json"
    assert result.is_file()


def test_create_uses_captured_source_after_trial_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = _experiment(tmp_path)
    trial = experiment / "baseline" / "task-1"
    displaced = experiment / "baseline" / "displaced-task"
    outside = tmp_path / "outside-task"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside-secret\n")
    output = tmp_path / "snapshot"
    original_build = create_module.build_extended_manifest
    swapped = False

    def swap_after_source_capture(*args: object, **kwargs: object) -> object:
        nonlocal swapped
        extended = original_build(*args, **kwargs)
        trial.rename(displaced)
        trial.symlink_to(outside, target_is_directory=True)
        swapped = True
        return extended

    monkeypatch.setattr(
        create_module,
        "build_extended_manifest",
        swap_after_source_capture,
    )

    create_snapshot(
        experiment,
        output,
        mode="contents",
        scanner=MockScanner(hit_substrings=[CANARY_DEFAULT]),
        allow_source_in_export=True,
    )

    exported = output / "export" / "traces" / "baseline" / "task-1"
    assert swapped is True
    assert (exported / "result.json").read_text() == '{"ok": true}\n'
    assert not (exported / "secret.txt").exists()


def test_create_attests_only_the_final_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = _experiment(tmp_path)
    redact_module = importlib.import_module("codeprobe.snapshot.redact")
    original_redact_attest = redact_module._attest
    original_create_attest = create_module._attest
    calls = 0

    def count_redact_attestation(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original_redact_attest(*args, **kwargs)

    def count_create_attestation(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original_create_attest(*args, **kwargs)

    monkeypatch.setattr(redact_module, "_attest", count_redact_attestation)
    monkeypatch.setattr(create_module, "_attest", count_create_attestation)

    create_snapshot(experiment, tmp_path / "snapshot")

    assert calls == 1


def test_create_rejects_in_place_snapshot_before_writing_layout(
    tmp_path: Path,
) -> None:
    experiment = _experiment(tmp_path)

    with pytest.raises(SymlinkEscapeError, match="in-place"):
        create_snapshot(experiment, experiment)

    assert not (experiment / "SNAPSHOT.json").exists()
    assert not (experiment / "summary").exists()
    assert not (experiment / "export").exists()
    assert not (experiment / "traces").exists()


def test_create_rejects_contained_source_symlink_before_output(
    tmp_path: Path,
) -> None:
    experiment = _experiment(tmp_path)
    result = experiment / "baseline" / "task-1" / "result.json"
    (experiment / "baseline" / "task-1" / "alias.json").symlink_to(result)
    output = tmp_path / "snapshot"

    with pytest.raises(SymlinkEscapeError, match="symlink"):
        create_snapshot(experiment, output)

    assert not output.exists()


@pytest.mark.parametrize("mode", ["hashes-only", "contents"])
def test_create_never_copies_unredacted_source_bodies(
    tmp_path: Path,
    mode: RedactionMode,
) -> None:
    secret = b"ghp_" + b"A" * 36
    experiment = _experiment(tmp_path)
    (experiment / "baseline" / "task-1" / "result.json").write_bytes(
        b'{"token":"' + secret + b'"}\n'
    )
    summary = experiment / "summary"
    summary.mkdir()
    (summary / "rewards.json").write_bytes(b'{"token":"' + secret + b'"}\n')
    output = tmp_path / "snapshot"

    if mode == "hashes-only":
        create_snapshot(experiment, output)
    else:
        create_snapshot(
            experiment,
            output,
            mode="contents",
            scanner=MockScanner(hit_substrings=[secret, CANARY_DEFAULT]),
            allow_source_in_export=True,
        )

    leaked_paths = [
        path.relative_to(output).as_posix()
        for path in output.rglob("*")
        if path.is_file() and secret in path.read_bytes()
    ]
    assert leaked_paths == []


def test_create_detects_manifest_replacement_after_secure_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = _experiment(tmp_path)
    output = tmp_path / "snapshot"
    victim = tmp_path / "victim.json"
    victim.write_text("outside-secret\n")
    original_write = safe_io.SecureOutputDirectory.write_bytes
    swapped = False

    def replace_manifest_after_write(
        directory: safe_io.SecureOutputDirectory,
        relative_path: str,
        data: bytes,
    ) -> Path:
        nonlocal swapped
        result = original_write(directory, relative_path, data)
        if relative_path == "SNAPSHOT.json":
            (directory.path / relative_path).unlink()
            (directory.path / relative_path).symlink_to(victim)
            swapped = True
        return result

    monkeypatch.setattr(
        safe_io.SecureOutputDirectory,
        "write_bytes",
        replace_manifest_after_write,
    )

    with pytest.raises(SymlinkEscapeError, match="file changed"):
        create_snapshot(experiment, output)

    assert swapped is True
    assert victim.read_text() == "outside-secret\n"


def test_create_detects_generated_symlink_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = _experiment(tmp_path)
    output = tmp_path / "snapshot"
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "secret.txt").write_text("outside-secret\n")
    original_symlink = safe_io.SecureOutputDirectory.symlink
    swapped = False

    def replace_generated_symlink(
        directory: safe_io.SecureOutputDirectory,
        relative_path: str,
        target: str,
    ) -> Path:
        nonlocal swapped
        result = original_symlink(directory, relative_path, target)
        (directory.path / relative_path).unlink()
        (directory.path / relative_path).symlink_to(
            victim,
            target_is_directory=True,
        )
        swapped = True
        return result

    monkeypatch.setattr(
        safe_io.SecureOutputDirectory,
        "symlink",
        replace_generated_symlink,
    )

    with pytest.raises(SymlinkEscapeError, match="symlink changed"):
        create_snapshot(experiment, output)

    assert swapped is True


def test_create_preserves_empty_trial_directories(tmp_path: Path) -> None:
    experiment = _experiment(tmp_path)
    (experiment / "baseline" / "empty-task").mkdir()
    (experiment / "mcp" / "empty-task").mkdir(parents=True)
    output = tmp_path / "snapshot"

    result = create_snapshot(experiment, output)

    assert result["traces"] == 3
    assert result["export_traces"] == 3
    assert (output / "export" / "traces" / "baseline" / "empty-task").is_dir()
    assert (output / "export" / "traces" / "mcp" / "empty-task").is_dir()


def test_create_rejects_symlinked_source_parent_before_fairness(
    tmp_path: Path,
) -> None:
    outside_parent = tmp_path / "outside"
    experiment = outside_parent / "experiment"
    trial = experiment / "baseline" / "task-1"
    trial.mkdir(parents=True)
    (trial / "result.json").write_text('{"ok": true}\n')
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside_parent, target_is_directory=True)
    output = tmp_path / "snapshot"

    with pytest.raises(SymlinkEscapeError):
        create_snapshot(
            linked_parent / "experiment",
            output,
            fairness_check=True,
        )

    assert not output.exists()


def test_create_cleans_staging_and_leaves_no_final_output_on_late_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = _experiment(tmp_path)
    output = tmp_path / "snapshot"
    original_write = safe_io.SecureOutputDirectory.write_bytes

    def fail_on_manifest(
        directory: safe_io.SecureOutputDirectory,
        relative_path: str,
        data: bytes,
    ) -> Path:
        if relative_path == "SNAPSHOT.json":
            raise OSError("injected late failure")
        return original_write(directory, relative_path, data)

    monkeypatch.setattr(
        safe_io.SecureOutputDirectory,
        "write_bytes",
        fail_on_manifest,
    )

    with pytest.raises(OSError, match="injected late failure"):
        create_snapshot(experiment, output)

    assert not output.exists()
    assert list(tmp_path.glob(".snapshot.tmp-*")) == []


def test_create_rejects_existing_destination_without_modifying_it(
    tmp_path: Path,
) -> None:
    experiment = _experiment(tmp_path)
    output = tmp_path / "snapshot"
    output.mkdir()
    marker = output / "owned.txt"
    marker.write_text("keep\n")

    with pytest.raises(SymlinkEscapeError, match="already exists"):
        create_snapshot(experiment, output)

    assert marker.read_text() == "keep\n"
    assert list(output.iterdir()) == [marker]


def test_atomic_publication_does_not_replace_raced_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = _experiment(tmp_path)
    output = tmp_path / "snapshot"
    original_publish = safe_io._rename_noreplace
    raced_inode: int | None = None

    def race_before_publish(
        source_name: str,
        destination_name: str,
        *,
        parent_fd: int,
    ) -> None:
        nonlocal raced_inode
        os.mkdir(destination_name, dir_fd=parent_fd)
        raced_inode = os.stat(
            destination_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        ).st_ino
        original_publish(
            source_name,
            destination_name,
            parent_fd=parent_fd,
        )

    monkeypatch.setattr(safe_io, "_rename_noreplace", race_before_publish)

    with pytest.raises(SymlinkEscapeError, match="already exists"):
        create_snapshot(experiment, output)

    assert raced_inode is not None
    assert output.stat().st_ino == raced_inode


def test_moved_staging_tree_is_erased_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    experiment = _experiment(tmp_path)
    output = tmp_path / "outputs" / "snapshot"
    moved = tmp_path / "moved-stage"
    original_write = safe_io.SecureOutputDirectory.write_bytes
    moved_stage = False

    def move_after_body(
        directory: safe_io.SecureOutputDirectory,
        relative_path: str,
        data: bytes,
    ) -> Path:
        nonlocal moved_stage
        result = original_write(directory, relative_path, data)
        if not moved_stage and relative_path.startswith("summary/"):
            directory.path.rename(moved)
            moved_stage = True
            raise OSError("injected failure after stage move")
        return result

    monkeypatch.setattr(
        safe_io.SecureOutputDirectory,
        "write_bytes",
        move_after_body,
    )

    with pytest.raises((OSError, SymlinkEscapeError)):
        create_snapshot(experiment, output)

    assert moved_stage is True
    assert not output.exists()
    assert moved.is_dir()
    assert list(moved.iterdir()) == []
