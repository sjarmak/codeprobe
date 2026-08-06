"""Regression tests for transactional mining corpus publication."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from codeprobe.mining import safe_output
from codeprobe.mining.comprehension import ComprehensionTaskSpec
from codeprobe.mining.comprehension_writer import write_comprehension_tasks
from codeprobe.mining.safe_output import ContainmentError
from codeprobe.mining.writer import write_task_dir
from codeprobe.models.task import Task, TaskMetadata, TaskVerification


def _task(task_id: str) -> Task:
    return Task(
        id=task_id,
        repo="example/repo",
        metadata=TaskMetadata(
            name=task_id,
            description="Replacement task",
            language="python",
        ),
        verification=TaskVerification(
            type="test_script",
            command="bash tests/test.sh",
            reward_type="binary",
        ),
    )


def _spec() -> ComprehensionTaskSpec:
    return ComprehensionTaskSpec(
        template="return_type_resolution",
        question="What does the function return?",
        answer="int",
        answer_type="text",
        target="example",
    )


def _seed_existing_task(task_dir: Path) -> dict[str, bytes]:
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("old instruction\n")
    (task_dir / "metadata.json").write_text('{"old": true}\n')
    (task_dir / "sentinel.txt").write_text("must survive\n")
    (tests_dir / "test.sh").write_text("#!/bin/sh\nexit 7\n")
    return _snapshot(task_dir)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_no_transaction_residue(output_dir: Path) -> None:
    assert all(
        not child.name.startswith(".codeprobe-")
        for child in output_dir.iterdir()
    )


def test_task_write_failure_preserves_existing_corpus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("atomic-task")
    output_dir = tmp_path / "tasks"
    before = _seed_existing_task(output_dir / task.id)

    def fail_late(*args: object, **kwargs: object) -> None:
        raise OSError("simulated late filesystem failure")

    monkeypatch.setattr("codeprobe.mining.writer._write_checkpoints", fail_late)

    with pytest.raises(OSError, match="simulated late filesystem failure"):
        write_task_dir(task, output_dir, tmp_path / "repo")

    assert _snapshot(output_dir / task.id) == before
    _assert_no_transaction_residue(output_dir)


def test_comprehension_batch_failure_preserves_every_existing_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = []
    for task_id in ("atomic-first", "atomic-second"):
        task = _task(task_id)
        tasks.append(
            replace(
                task,
                metadata=replace(
                    task.metadata,
                    category="architecture_comprehension",
                    task_type="architecture_comprehension",
                ),
            )
        )
    output_dir = tmp_path / "tasks"
    before = {
        task.id: _seed_existing_task(output_dir / task.id) for task in tasks
    }
    calls = 0

    def fail_on_second_task(*args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated late batch failure")

    monkeypatch.setattr(
        "codeprobe.mining.comprehension_writer._write_checkpoints",
        fail_on_second_task,
    )

    with pytest.raises(OSError, match="simulated late batch failure"):
        write_comprehension_tasks(
            tasks,
            output_dir,
            specs={task.id: _spec() for task in tasks},
        )

    assert {
        task.id: _snapshot(output_dir / task.id) for task in tasks
    } == before
    _assert_no_transaction_residue(output_dir)


def test_comprehension_publish_failure_rolls_back_earlier_swaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tasks = []
    for task_id in ("publish-first", "publish-second"):
        task = _task(task_id)
        tasks.append(
            replace(
                task,
                metadata=replace(
                    task.metadata,
                    category="architecture_comprehension",
                    task_type="architecture_comprehension",
                ),
            )
        )
    output_dir = tmp_path / "tasks"
    before = {
        task.id: _seed_existing_task(output_dir / task.id) for task in tasks
    }
    real_rename = safe_output._rename_component_no_replace

    def fail_second_publish(
        parent_fd: int,
        source: str,
        destination: str,
    ) -> None:
        if (
            source.startswith(".codeprobe-stage-")
            and destination == tasks[1].id
        ):
            raise OSError("simulated publish rename failure")
        real_rename(parent_fd, source, destination)

    monkeypatch.setattr(
        safe_output,
        "_rename_component_no_replace",
        fail_second_publish,
    )

    with pytest.raises(OSError, match="simulated publish rename failure"):
        write_comprehension_tasks(
            tasks,
            output_dir,
            specs={task.id: _spec() for task in tasks},
        )

    assert {
        task.id: _snapshot(output_dir / task.id) for task in tasks
    } == before
    _assert_no_transaction_residue(output_dir)


def test_batch_setup_failure_removes_earlier_stages(
    tmp_path: Path,
) -> None:
    tasks = []
    for task_id in ("setup-first", "setup-second"):
        task = _task(task_id)
        tasks.append(
            replace(
                task,
                metadata=replace(
                    task.metadata,
                    category="architecture_comprehension",
                    task_type="architecture_comprehension",
                ),
            )
        )
    output_dir = tmp_path / "tasks"
    output_dir.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel.txt").write_text("must survive\n")
    (output_dir / tasks[1].id).symlink_to(external, target_is_directory=True)

    with pytest.raises(ContainmentError):
        write_comprehension_tasks(
            tasks,
            output_dir,
            specs={task.id: _spec() for task in tasks},
        )

    assert (external / "sentinel.txt").read_text() == "must survive\n"
    _assert_no_transaction_residue(output_dir)


def test_destination_root_swap_preserves_containment_error_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = _task("swap-root")
    output_dir = tmp_path / "tasks"
    _seed_existing_task(output_dir / task.id)
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel.txt").write_text("must survive\n")
    displaced = output_dir / f"{task.id}-displaced"
    real_stat = safe_output.os.stat
    swapped = False

    def swap_after_root_stat(
        path: os.PathLike[str] | str | int,
        *args: object,
        **kwargs: object,
    ) -> os.stat_result:
        nonlocal swapped
        info = real_stat(path, *args, **kwargs)
        if path == task.id and not swapped:
            swapped = True
            os.rename(output_dir / task.id, displaced)
            os.symlink(external, output_dir / task.id)
        return info

    monkeypatch.setattr(safe_output.os, "stat", swap_after_root_stat)

    with pytest.raises(ContainmentError):
        write_task_dir(task, output_dir, tmp_path / "repo")

    assert (external / "sentinel.txt").read_text() == "must survive\n"
    _assert_no_transaction_residue(output_dir)


def test_private_stage_swap_preserves_containment_error_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "tasks"
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel.txt").write_text("must survive\n")
    real_mkdir = safe_output.os.mkdir

    def swap_private_stage(
        path: os.PathLike[str] | str,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        real_mkdir(path, mode, dir_fd=dir_fd)
        if (
            isinstance(path, str)
            and path.startswith(".codeprobe-stage-")
            and dir_fd is not None
        ):
            os.rmdir(path, dir_fd=dir_fd)
            os.symlink(external, path, dir_fd=dir_fd)

    monkeypatch.setattr(safe_output.os, "mkdir", swap_private_stage)

    with pytest.raises(ContainmentError):
        with safe_output.staged_output_dirs(output_dir, ["task"]):
            pytest.fail("a swapped private stage must never be yielded")

    assert (external / "sentinel.txt").read_text() == "must survive\n"
    conflicts = list(output_dir.glob(".codeprobe-stage-*"))
    assert len(conflicts) == 1
    assert conflicts[0].is_symlink()


def test_swapped_real_stage_with_symlink_is_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "tasks"
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel.txt").write_text("must survive\n")
    real_mkdir = safe_output.os.mkdir

    def replace_private_stage_with_attacker_dir(
        path: os.PathLike[str] | str,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        real_mkdir(path, mode, dir_fd=dir_fd)
        if (
            isinstance(path, str)
            and path.startswith(".codeprobe-stage-")
            and dir_fd is not None
        ):
            os.rmdir(path, dir_fd=dir_fd)
            real_mkdir(path, mode, dir_fd=dir_fd)
            os.symlink(external, f"{path}/escape", dir_fd=dir_fd)

    monkeypatch.setattr(
        safe_output.os,
        "mkdir",
        replace_private_stage_with_attacker_dir,
    )

    with pytest.raises(ContainmentError):
        with safe_output.staged_output_dirs(output_dir, ["task"]) as staged:
            staged["task"].write_text("artifact.txt", "payload\n")

    assert not (output_dir / "task").exists()
    assert (external / "sentinel.txt").read_text() == "must survive\n"
    _assert_no_transaction_residue(output_dir)


def test_stage_swap_during_publish_preserves_conflict_and_original_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "tasks"
    task_dir = output_dir / "task"
    before = _seed_existing_task(task_dir)
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel.txt").write_text("must survive\n")
    real_rename = safe_output._rename_component_no_replace
    swapped = False

    def swap_immediately_before_publish(
        parent_fd: int,
        source: str,
        destination: str,
    ) -> None:
        nonlocal swapped
        if source.startswith(".codeprobe-stage-") and not swapped:
            swapped = True
            # Build the replacement BEFORE freeing the original, so the
            # filesystem cannot hand back the inode it just released.
            # Rollback keys on (st_dev, st_ino): under ext4 (the CI
            # runners) a remove-then-mkdir reuses the inode, the swapped
            # directory is indistinguishable from the staged one it
            # replaced, rollback succeeds, and this test's premise —
            # an unreconcilable component — never happens. xfs allocates
            # a fresh inode, which is why it passed locally and failed in
            # CI (codeprobe-7b0e).
            decoy = ".swapped-by-test"
            os.mkdir(decoy, 0o700, dir_fd=parent_fd)
            os.symlink(external, f"{decoy}/escape", dir_fd=parent_fd)
            before_ino = os.stat(source, dir_fd=parent_fd).st_ino
            safe_output._remove_tree_at(parent_fd, source)
            os.rename(decoy, source, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            assert os.stat(source, dir_fd=parent_fd).st_ino != before_ino, (
                "the swap must produce a distinct inode or the test is "
                "asserting the wrong failure path"
            )
        real_rename(parent_fd, source, destination)

    monkeypatch.setattr(
        safe_output,
        "_rename_component_no_replace",
        swap_immediately_before_publish,
    )

    with pytest.raises(BaseExceptionGroup):
        with safe_output.staged_output_dirs(output_dir, ["task"]) as staged:
            staged["task"].write_text("artifact.txt", "replacement\n")

    assert (task_dir / "escape").is_symlink()
    backups = list(output_dir.glob(".codeprobe-backup-*"))
    assert len(backups) == 1
    assert _snapshot(backups[0]) == before
    assert (external / "sentinel.txt").read_text() == "must survive\n"
    assert not list(output_dir.glob(".codeprobe-stage-*"))


def test_interrupt_after_successful_rename_restores_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "tasks"
    task_dir = output_dir / "task"
    before = _seed_existing_task(task_dir)
    real_rename = safe_output._rename_component_no_replace
    interrupted = False

    def interrupt_after_publish_rename(
        parent_fd: int,
        source: str,
        destination: str,
    ) -> None:
        nonlocal interrupted
        real_rename(parent_fd, source, destination)
        if source.startswith(".codeprobe-stage-") and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(
        safe_output,
        "_rename_component_no_replace",
        interrupt_after_publish_rename,
    )

    with pytest.raises(KeyboardInterrupt):
        with safe_output.staged_output_dirs(output_dir, ["task"]) as staged:
            staged["task"].write_text("artifact.txt", "replacement\n")

    assert _snapshot(task_dir) == before
    _assert_no_transaction_residue(output_dir)


def test_interrupt_after_backup_rename_restores_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "tasks"
    task_dir = output_dir / "task"
    before = _seed_existing_task(task_dir)
    real_rename = safe_output._rename_component
    interrupted = False

    def interrupt_after_backup_rename(
        parent_fd: int,
        source: str,
        destination: str,
    ) -> None:
        nonlocal interrupted
        real_rename(parent_fd, source, destination)
        if source == "task" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    monkeypatch.setattr(
        safe_output,
        "_rename_component",
        interrupt_after_backup_rename,
    )

    with pytest.raises(KeyboardInterrupt):
        with safe_output.staged_output_dirs(output_dir, ["task"]) as staged:
            staged["task"].write_text("artifact.txt", "replacement\n")

    assert _snapshot(task_dir) == before
    _assert_no_transaction_residue(output_dir)


def test_rollback_preserves_unrelated_concurrent_final(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "tasks"
    task_dir = output_dir / "task"
    before = _seed_existing_task(task_dir)
    real_rename = safe_output._rename_component_no_replace
    replaced = False

    def replace_published_stage_with_concurrent_data(
        parent_fd: int,
        source: str,
        destination: str,
    ) -> None:
        nonlocal replaced
        real_rename(parent_fd, source, destination)
        if source.startswith(".codeprobe-stage-") and not replaced:
            replaced = True
            # Same inode-reuse hazard as the stage-swap test: build the
            # concurrent directory before freeing the published one, or
            # ext4 hands back the same inode, rollback cannot tell this
            # apart from what it published, and nothing raises at all
            # (codeprobe-7b0e).
            decoy = ".concurrent-by-test"
            os.mkdir(decoy, 0o700, dir_fd=parent_fd)
            concurrent_fd = os.open(
                f"{decoy}/concurrent.txt",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            os.close(concurrent_fd)
            before_ino = os.stat(destination, dir_fd=parent_fd).st_ino
            safe_output._remove_tree_at(parent_fd, destination)
            os.rename(
                decoy, destination, src_dir_fd=parent_fd, dst_dir_fd=parent_fd
            )
            assert os.stat(destination, dir_fd=parent_fd).st_ino != before_ino, (
                "the concurrent replacement must have a distinct inode or "
                "the test is asserting the wrong failure path"
            )

    monkeypatch.setattr(
        safe_output,
        "_rename_component_no_replace",
        replace_published_stage_with_concurrent_data,
    )

    with pytest.raises(BaseExceptionGroup):
        with safe_output.staged_output_dirs(output_dir, ["task"]) as staged:
            staged["task"].write_text("artifact.txt", "replacement\n")

    assert (task_dir / "concurrent.txt").exists()
    backups = list(output_dir.glob(".codeprobe-backup-*"))
    assert len(backups) == 1
    assert _snapshot(backups[0]) == before


def test_atomic_publish_never_replaces_concurrent_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "tasks"
    task_dir = output_dir / "task"
    before = _seed_existing_task(task_dir)
    real_publish = safe_output._rename_component_no_replace
    concurrent_inode: int | None = None

    def plant_destination_before_publish(
        parent_fd: int,
        source: str,
        destination: str,
    ) -> None:
        nonlocal concurrent_inode
        if source.startswith(".codeprobe-stage-") and destination == "task":
            os.mkdir(destination, 0o700, dir_fd=parent_fd)
            concurrent_inode = os.stat(
                destination,
                dir_fd=parent_fd,
                follow_symlinks=False,
            ).st_ino
        real_publish(parent_fd, source, destination)

    monkeypatch.setattr(
        safe_output,
        "_rename_component_no_replace",
        plant_destination_before_publish,
    )

    with pytest.raises(BaseExceptionGroup):
        with safe_output.staged_output_dirs(output_dir, ["task"]) as staged:
            staged["task"].write_text("artifact.txt", "replacement\n")

    assert task_dir.stat().st_ino == concurrent_inode
    backups = list(output_dir.glob(".codeprobe-backup-*"))
    assert len(backups) == 1
    assert _snapshot(backups[0]) == before
    assert not list(output_dir.glob(".codeprobe-stage-*"))


def test_cleanup_preserves_unrelated_component_at_stage_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_dir = tmp_path / "tasks"
    names = ("first", "second")
    for name in names:
        _seed_existing_task(output_dir / name)
    real_publish = safe_output._rename_component_no_replace
    calls = 0
    planted_name: str | None = None

    def plant_stage_conflict_then_fail(
        parent_fd: int,
        source: str,
        destination: str,
    ) -> None:
        nonlocal calls, planted_name
        if destination not in names:
            real_publish(parent_fd, source, destination)
            return
        calls += 1
        if calls == 1:
            real_publish(parent_fd, source, destination)
            os.mkdir(source, 0o700, dir_fd=parent_fd)
            planted_name = source
            conflict_fd = os.open(
                f"{source}/concurrent.txt",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            os.close(conflict_fd)
            return
        raise OSError("force later publish failure")

    monkeypatch.setattr(
        safe_output,
        "_rename_component_no_replace",
        plant_stage_conflict_then_fail,
    )

    with pytest.raises(BaseExceptionGroup):
        with safe_output.staged_output_dirs(output_dir, list(names)) as staged:
            for name in names:
                staged[name].write_text("artifact.txt", "replacement\n")

    assert planted_name is not None
    assert (output_dir / planted_name / "concurrent.txt").exists()


def test_cleanup_atomically_claims_expected_inode_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base_dir = tmp_path / "tasks"
    owned = base_dir / "owned"
    owned.mkdir(parents=True)
    (owned / "artifact.txt").write_text("transaction data\n")
    expected = (owned.stat().st_dev, owned.stat().st_ino)
    displaced = base_dir / "owned-displaced"
    real_claim = safe_output._rename_component_no_replace
    swapped = False

    def swap_before_cleanup_claim(
        parent_fd: int,
        source: str,
        destination: str,
    ) -> None:
        nonlocal swapped
        if source == "owned" and not swapped:
            swapped = True
            os.rename(
                source,
                displaced.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            os.mkdir(source, 0o700, dir_fd=parent_fd)
            concurrent_fd = os.open(
                f"{source}/concurrent.txt",
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_fd,
            )
            os.close(concurrent_fd)
        real_claim(parent_fd, source, destination)

    monkeypatch.setattr(
        safe_output,
        "_rename_component_no_replace",
        swap_before_cleanup_claim,
    )
    base_fd = os.open(base_dir, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(ContainmentError):
            safe_output._remove_owned_component(base_fd, "owned", expected)
    finally:
        os.close(base_fd)

    assert (owned / "concurrent.txt").exists()
    assert (displaced / "artifact.txt").read_text() == "transaction data\n"
