"""Adversarial symlink-containment tests for the mining writers (codeprobe-2cqg).

A reused output tree may hold a pre-existing symlink (left by a prior run or
planted by an attacker with control of the tree). Every mining writer must
fail closed rather than follow it, so no task, metadata, ground-truth,
divergence, checkpoint, or verifier artifact is created outside the
caller-supplied base directory (CWE-59).

Boundaries exercised:

1. ``write_task_dir``            — task dir, tests dir, and leaf-file symlinks
2. ``write_quarantined_task``    — task dir symlink
3. ``write_comprehension_tasks`` — task dir and tests dir symlinks
4. checkpoint verifier emission  — ``tests/verifiers`` dir symlink

plus a check/use swap at the ``SafeOutputDir`` level proving writes bind to
the opened descriptor, not the re-resolved path string.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeprobe.mining.comprehension import (
    ComprehensionGenerator,
    write_comprehension_tasks,
)
from codeprobe.mining.org_scale import _change_scope_checkpoints
from codeprobe.mining.safe_output import ContainmentError, SafeOutputDir
from codeprobe.mining.writer import (
    _write_checkpoints,
    write_quarantined_task,
    write_task_dir,
)
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

_SENTINEL = "external-content-must-not-change\n"


def _external_sentinel_dir(tmp_path: Path) -> Path:
    """A directory outside any base dir, seeded with a sentinel file."""
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel.txt").write_text(_SENTINEL, encoding="utf-8")
    return external


def _assert_dir_unchanged(external: Path, before: set[str]) -> None:
    """The external directory gained no files and its sentinel is intact."""
    after = {p.name for p in external.iterdir()}
    assert after == before, f"external dir gained artifacts: {after - before}"
    assert (external / "sentinel.txt").read_text(encoding="utf-8") == _SENTINEL


def _simple_task(task_id: str = "task-abc12345") -> Task:
    return Task(
        id=task_id,
        repo="example/repo",
        metadata=TaskMetadata(
            name=task_id,
            description="Fix the thing.",
            language="python",
        ),
        verification=TaskVerification(
            type="test_script",
            command="bash tests/test.sh",
            reward_type="binary",
        ),
    )


def _change_scope_task(task_id: str = "change-scope-abc12345") -> Task:
    """An oracle task that emits ``tests/verifiers/`` checkpoint scripts."""
    return Task(
        id=task_id,
        repo="example/repo",
        metadata=TaskMetadata(
            name=task_id,
            category="change-scope-audit",
            org_scale=True,
            issue_title="Blast radius of changing Foo",
            issue_body="Find all files that depend on Foo.",
            ground_truth_commit="deadbeef",
            language="python",
        ),
        verification=TaskVerification(
            type="oracle",
            command="bash tests/test.sh",
            reward_type="continuous",
            oracle_type="file_list",
            oracle_answer=("src/a.py", "src/b.py"),
            checkpoints=_change_scope_checkpoints(),
        ),
    )


# ---------------------------------------------------------------------------
# Boundary 1: write_task_dir
# ---------------------------------------------------------------------------


class TestWriteTaskDirContainment:
    def test_rejects_symlinked_task_dir(self, tmp_path: Path) -> None:
        external = _external_sentinel_dir(tmp_path)
        before = {p.name for p in external.iterdir()}
        base_dir = tmp_path / "tasks"
        base_dir.mkdir()
        task = _simple_task()
        # base_dir/<task.id> is a pre-existing symlink into the external tree.
        os.symlink(external, base_dir / task.id)

        with pytest.raises(ContainmentError) as excinfo:
            write_task_dir(task, base_dir, tmp_path / "repo")

        _assert_dir_unchanged(external, before)
        # Error names the component/base, never the symlink target (no leak).
        assert str(external) not in str(excinfo.value)
        assert isinstance(excinfo.value, ValueError)

    def test_rejects_symlinked_tests_dir(self, tmp_path: Path) -> None:
        external = _external_sentinel_dir(tmp_path)
        before = {p.name for p in external.iterdir()}
        base_dir = tmp_path / "tasks"
        task = _simple_task()
        task_dir = base_dir / task.id
        task_dir.mkdir(parents=True)
        os.symlink(external, task_dir / "tests")

        with pytest.raises(ContainmentError):
            write_task_dir(task, base_dir, tmp_path / "repo")

        _assert_dir_unchanged(external, before)

    def test_rejects_symlinked_leaf_file(self, tmp_path: Path) -> None:
        external_file = tmp_path / "outside.md"
        external_file.write_text(_SENTINEL, encoding="utf-8")
        base_dir = tmp_path / "tasks"
        task = _simple_task()
        task_dir = base_dir / task.id
        task_dir.mkdir(parents=True)
        # A pre-existing symlinked leaf must not be written through.
        os.symlink(external_file, task_dir / "instruction.md")

        with pytest.raises(ContainmentError):
            write_task_dir(task, base_dir, tmp_path / "repo")

        assert external_file.read_text(encoding="utf-8") == _SENTINEL

    def test_rejects_symlinked_confidence_json(self, tmp_path: Path) -> None:
        """write_task_dir emits confidence.json last; a symlinked target is an
        attack indicator that must fail closed (ContainmentError propagates)
        without writing through the link."""
        external_file = tmp_path / "outside.json"
        external_file.write_text(_SENTINEL, encoding="utf-8")
        base_dir = tmp_path / "tasks"
        task = _simple_task()
        task_dir = base_dir / task.id
        task_dir.mkdir(parents=True)
        os.symlink(external_file, task_dir / "confidence.json")

        with pytest.raises(ContainmentError):
            write_task_dir(task, base_dir, tmp_path / "repo")

        assert external_file.read_text(encoding="utf-8") == _SENTINEL

    def test_safe_flow_still_writes(self, tmp_path: Path) -> None:
        """The fix must not break a clean write into a fresh base dir."""
        base_dir = tmp_path / "tasks"
        task = _simple_task()
        result = write_task_dir(task, base_dir, tmp_path / "repo")
        assert result == base_dir / task.id
        assert (result / "instruction.md").is_file()
        assert (result / "confidence.json").is_file()
        test_sh = result / "tests" / "test.sh"
        assert test_sh.is_file()
        assert test_sh.stat().st_mode & 0o111, "test.sh must stay executable"


# ---------------------------------------------------------------------------
# Boundary 2: write_quarantined_task
# ---------------------------------------------------------------------------


class TestWriteQuarantinedTaskContainment:
    def test_rejects_symlinked_task_dir(self, tmp_path: Path) -> None:
        external = _external_sentinel_dir(tmp_path)
        before = {p.name for p in external.iterdir()}
        base_dir = tmp_path / "quarantine"
        base_dir.mkdir()
        task_id = "quarantined-abc12345"
        os.symlink(external, base_dir / task_id)

        with pytest.raises(ContainmentError):
            write_quarantined_task(
                task_id=task_id,
                family="consensus",
                repo="example/repo",
                symbol="Foo",
                defining_file="src/foo.py",
                instruction_title="t",
                instruction_body="b",
                divergence_report={"schema": "consensus.v1"},
                base_dir=base_dir,
            )

        _assert_dir_unchanged(external, before)


# ---------------------------------------------------------------------------
# Boundary 3: write_comprehension_tasks
# ---------------------------------------------------------------------------


@pytest.fixture
def chain_repo(tmp_path: Path) -> Path:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "a.py").write_text("from pkg import b\n\ndef use_a():\n    return b.foo_b()\n")
    (pkg / "b.py").write_text("from pkg import c\n\ndef foo_b():\n    return c.foo()\n")
    (pkg / "c.py").write_text("def foo() -> int:\n    return 42\n")
    (pkg / "d.py").write_text("from pkg import a\n\ndef runner():\n    return a.use_a()\n")
    return tmp_path


class TestWriteComprehensionContainment:
    def test_rejects_symlinked_task_dir(
        self, chain_repo: Path, tmp_path: Path
    ) -> None:
        tasks = ComprehensionGenerator(chain_repo).generate(count=4)
        assert tasks, "need at least one comprehension task"
        task = tasks[0]

        external = _external_sentinel_dir(tmp_path)
        before = {p.name for p in external.iterdir()}
        out = tmp_path / "out"
        out.mkdir()
        os.symlink(external, out / task.id)

        with pytest.raises(ContainmentError):
            write_comprehension_tasks([task], out)

        _assert_dir_unchanged(external, before)

    def test_rejects_symlinked_tests_dir(
        self, chain_repo: Path, tmp_path: Path
    ) -> None:
        tasks = ComprehensionGenerator(chain_repo).generate(count=4)
        assert tasks
        task = tasks[0]

        external = _external_sentinel_dir(tmp_path)
        before = {p.name for p in external.iterdir()}
        out = tmp_path / "out"
        task_dir = out / task.id
        task_dir.mkdir(parents=True)
        os.symlink(external, task_dir / "tests")

        with pytest.raises(ContainmentError):
            write_comprehension_tasks([task], out)

        _assert_dir_unchanged(external, before)


# ---------------------------------------------------------------------------
# Boundary 4: checkpoint verifier emission (tests/verifiers/)
# ---------------------------------------------------------------------------


class TestCheckpointVerifierContainment:
    def test_rejects_symlinked_verifiers_dir(self, tmp_path: Path) -> None:
        external = _external_sentinel_dir(tmp_path)
        before = {p.name for p in external.iterdir()}
        base_dir = tmp_path / "tasks"
        task = _change_scope_task()
        tests_dir = base_dir / task.id / "tests"
        tests_dir.mkdir(parents=True)
        os.symlink(external, tests_dir / "verifiers")

        repo_path = tmp_path / "repo"
        repo_path.mkdir()

        with pytest.raises(ContainmentError):
            write_task_dir(task, base_dir, repo_path)

        _assert_dir_unchanged(external, before)

    def test_cleanup_swap_does_not_delete_outside_base(
        self, tmp_path: Path
    ) -> None:
        """The no-checkpoints cleanup path must delete via the held tests
        descriptor, not the path string. A swapped ``tests`` symlink must not
        redirect the removal into an external tree (codeprobe-2cqg)."""
        base_dir = tmp_path / "base"
        base_dir.mkdir()

        # External tree the attacker points the swapped symlink at.
        external = tmp_path / "external"
        (external / "verifiers").mkdir(parents=True)
        (external / "checkpoints.json").write_text(_SENTINEL, encoding="utf-8")
        (external / "verifiers" / "e.sh").write_text(_SENTINEL, encoding="utf-8")

        with SafeOutputDir.create(base_dir, "t") as task_out:
            tests_out = task_out.child("tests")
            try:
                # Stale checkpoint artifacts in the REAL tests dir.
                tests_out.write_text("checkpoints.json", "[]\n")
                with tests_out.child("verifiers") as v:
                    v.write_text("old.sh", "#!/bin/bash\n", executable=True)

                # Swap: move the real tests dir aside, replace with a symlink
                # to the external tree. tests_out still holds the real fd.
                os.rename(base_dir / "t" / "tests", base_dir / "t" / "tests_real")
                os.symlink(external, base_dir / "t" / "tests")

                # No-checkpoints task → cleanup branch runs.
                _write_checkpoints(_simple_task(), tests_out, None)
            finally:
                tests_out.close()

        # External tree is untouched.
        assert (external / "checkpoints.json").read_text(
            encoding="utf-8"
        ) == _SENTINEL
        assert (external / "verifiers" / "e.sh").exists()
        # Stale artifacts in the real tests dir were actually removed.
        real_tests = base_dir / "t" / "tests_real"
        assert not (real_tests / "checkpoints.json").exists()
        assert not (real_tests / "verifiers").exists()


# ---------------------------------------------------------------------------
# check/use swap: writes bind to the opened descriptor, not the path string
# ---------------------------------------------------------------------------


class TestSafeOutputDirSwap:
    def test_write_follows_fd_not_swapped_path(self, tmp_path: Path) -> None:
        external = tmp_path / "external"
        external.mkdir()
        base_dir = tmp_path / "base"
        base_dir.mkdir()

        with SafeOutputDir.create(base_dir, "t") as handle:
            # Swap the path out from under the held descriptor: move the real
            # directory aside and drop a symlink to the external tree in its
            # place. A path-string writer would now publish into external/.
            os.rename(base_dir / "t", base_dir / "t_real")
            os.symlink(external, base_dir / "t")

            handle.write_text("artifact.txt", "payload")

        # The write followed the descriptor to the original inode, not the
        # swapped-in symlink.
        assert (base_dir / "t_real" / "artifact.txt").read_text() == "payload"
        assert not (external / "artifact.txt").exists()

    def test_create_rejects_symlink_component(self, tmp_path: Path) -> None:
        external = tmp_path / "external"
        external.mkdir()
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        os.symlink(external, base_dir / "t")

        with pytest.raises(ContainmentError):
            SafeOutputDir.create(base_dir, "t")

    @pytest.mark.parametrize("bad", ["..", ".", "a/b", ""])
    def test_write_rejects_unsafe_component(
        self, tmp_path: Path, bad: str
    ) -> None:
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        with SafeOutputDir.create(base_dir, "t") as handle:
            with pytest.raises(ContainmentError):
                handle.write_text(bad, "x")

    def test_write_rejects_fifo_leaf_without_hanging(
        self, tmp_path: Path
    ) -> None:
        """A pre-existing FIFO at the leaf name must be refused, not block
        forever on open (O_NONBLOCK + regular-file check)."""
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        with SafeOutputDir.create(base_dir, "t") as handle:
            os.mkfifo(handle.path / "pipe")
            with pytest.raises(ContainmentError):
                handle.write_text("pipe", "payload")

    def test_write_rejects_hardlinked_leaf_before_truncation(
        self, tmp_path: Path
    ) -> None:
        """A leaf hardlinked to an external file must be refused BEFORE the
        shared inode is truncated or overwritten. O_NOFOLLOW does not catch
        hardlinks, so the st_nlink check + deferred truncation guard it."""
        external_file = tmp_path / "outside.txt"
        external_file.write_text(_SENTINEL, encoding="utf-8")
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        with SafeOutputDir.create(base_dir, "t") as handle:
            # Plant a hardlink (shares the external inode; not a symlink).
            os.link(external_file, handle.path / "artifact.txt")
            with pytest.raises(ContainmentError):
                handle.write_text("artifact.txt", "payload")

        # The external inode was neither truncated nor overwritten.
        assert external_file.read_text(encoding="utf-8") == _SENTINEL

    def test_open_operational_error_is_not_containment(
        self, tmp_path: Path
    ) -> None:
        """A permission error must propagate as OSError, not be masked as a
        ContainmentError (errno narrowing). Skipped when running as root,
        which ignores the mode bits."""
        if os.geteuid() == 0:
            pytest.skip("root ignores directory write permission bits")
        base_dir = tmp_path / "base"
        base_dir.mkdir()
        handle = SafeOutputDir.create(base_dir, "t")
        try:
            os.chmod(handle.path, 0o500)  # read/execute only, no write
            with pytest.raises(OSError) as excinfo:
                handle.write_text("x.txt", "payload")
            assert not isinstance(excinfo.value, ContainmentError)
        finally:
            os.chmod(handle.path, 0o700)
            handle.close()
