"""Tests for codeprobe.mining.refresh (R20)."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest

from codeprobe.mining.refresh import (
    CHURN_THRESHOLD,
    RefreshDiff,
    StructuralMismatchError,
    StructuralSignature,
    compute_diff,
    jaccard,
    read_structural_signature,
    refresh_task,
    signature_from_task,
)
from codeprobe.mining.writer import write_task_dir
from codeprobe.models.task import Task, TaskMetadata, TaskVerification


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _oracle_task(
    task_id: str = "tsk-0001",
    files: tuple[str, ...] = ("src/a.py", "src/b.py", "src/c.py"),
    oracle_type: str = "file_list",
    commit: str = "aaa0000",
    history: tuple[str, ...] = (),
) -> Task:
    """Build an oracle-style Task with a file_list ground truth."""
    return Task(
        id=task_id,
        repo="example/repo",
        metadata=TaskMetadata(
            name=task_id,
            description=f"desc for {task_id}",
            category="symbol-reference-trace",
            task_type="org_scale_cross_repo",
            org_scale=True,
            language="python",
            ground_truth_commit=commit,
            ground_truth_commit_history=history,
        ),
        verification=TaskVerification(
            type="oracle",
            command="python3 tests/oracle.py .",
            oracle_type=oracle_type,
            oracle_answer=files,
        ),
    )


def _write_existing_task_dir(
    tmp_path: Path,
    task: Task,
    repo_path: Path | None = None,
) -> Path:
    """Write a task dir to disk via the real writer. Returns the task dir."""
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir(exist_ok=True)
    rp = repo_path if repo_path is not None else tmp_path / "repo"
    rp.mkdir(exist_ok=True)
    return write_task_dir(task, tasks_dir, rp)


# ---------------------------------------------------------------------------
# Jaccard / compute_diff
# ---------------------------------------------------------------------------


class TestJaccardAndDiff:
    def test_jaccard_empty_sets(self) -> None:
        assert jaccard(set(), set()) == 1.0

    def test_jaccard_disjoint(self) -> None:
        assert jaccard({"a"}, {"b"}) == 0.0

    def test_jaccard_identical(self) -> None:
        assert jaccard({"a", "b"}, {"a", "b"}) == 1.0

    def test_jaccard_half_overlap(self) -> None:
        # {a,b} vs {b,c} → |∩|=1, |∪|=3 → 1/3
        assert jaccard({"a", "b"}, {"b", "c"}) == pytest.approx(1.0 / 3.0)

    def test_compute_diff_identical_signatures(self) -> None:
        sig = StructuralSignature(
            oracle_type="file_list", oracle_files=("a", "b", "c")
        )
        diff = compute_diff(sig, sig)
        assert diff.churn == 0.0
        assert not diff.oracle_type_changed
        assert not diff.is_structural_mismatch
        assert diff.added_files == ()
        assert diff.removed_files == ()

    def test_compute_diff_type_change(self) -> None:
        old = StructuralSignature(oracle_type="file_list", oracle_files=("a",))
        new = StructuralSignature(oracle_type="count", oracle_files=("a",))
        diff = compute_diff(old, new)
        assert diff.oracle_type_changed
        assert diff.is_structural_mismatch

    def test_churn_boundary_at_exactly_20_percent_passes(self) -> None:
        # 5 files, replace 1 → |∩|=4, |∪|=6 → jaccard=4/6=0.667, churn=0.333
        # We want to hit exactly the 0.20 boundary: use 9 files, replace 1.
        # |∩|=8, |∪|=10 → jaccard=0.8, churn=0.20.
        old_files = tuple(f"f{i}.py" for i in range(9))
        new_files = tuple(f"f{i}.py" for i in range(1, 9)) + ("new.py",)
        old_sig = StructuralSignature("file_list", old_files)
        new_sig = StructuralSignature("file_list", new_files)
        diff = compute_diff(old_sig, new_sig)
        assert diff.churn == pytest.approx(0.20)
        # Threshold is strict '>', so exactly-0.20 is allowed.
        assert not diff.is_structural_mismatch

    def test_churn_above_threshold_flagged(self) -> None:
        # 5 files, replace 2 → |∩|=3, |∪|=7 → jaccard=3/7=0.4286, churn≈0.571
        old_files = ("a", "b", "c", "d", "e")
        new_files = ("a", "b", "c", "x", "y")
        old_sig = StructuralSignature("file_list", old_files)
        new_sig = StructuralSignature("file_list", new_files)
        diff = compute_diff(old_sig, new_sig)
        assert diff.churn > CHURN_THRESHOLD
        assert diff.is_structural_mismatch

    def test_as_report_without_task_dir_uses_placeholder(self) -> None:
        old = StructuralSignature("file_list", ("a", "b"))
        new = StructuralSignature("count", ("a", "b"))
        report = compute_diff(old, new).as_report()
        assert "codeprobe mine --refresh" in report
        assert "--accept-structural-change" in report
        # Without a concrete task_dir, the report uses a placeholder.
        assert "<task_dir>" in report

    def test_as_report_with_task_dir_names_exact_command(self) -> None:
        old = StructuralSignature("file_list", ("a", "b"))
        new = StructuralSignature("count", ("a", "b"))
        report = compute_diff(old, new).as_report("/path/to/tasks/tsk-0001")
        assert "/path/to/tasks/tsk-0001" in report
        assert "codeprobe mine --refresh /path/to/tasks/tsk-0001 --accept-structural-change" in report


# ---------------------------------------------------------------------------
# Signature extraction from disk
# ---------------------------------------------------------------------------


class TestReadSignature:
    def test_read_oracle_signature(self, tmp_path: Path) -> None:
        task = _oracle_task(files=("src/a.py", "src/b.py"))
        task_dir = _write_existing_task_dir(tmp_path, task)
        sig = read_structural_signature(task_dir)
        assert sig.oracle_type == "file_list"
        assert sig.oracle_files == ("src/a.py", "src/b.py")

    def test_read_dual_signature(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "t"
        (task_dir / "tests").mkdir(parents=True)
        (task_dir / "tests" / "ground_truth.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "answer_type": "file_list",
                    "answer": ["src/x.py", "src/y.py"],
                }
            )
        )
        (task_dir / "metadata.json").write_text(
            json.dumps({"id": "t", "metadata": {}, "verification": {}})
        )
        sig = read_structural_signature(task_dir)
        assert sig.oracle_type == "file_list"
        assert sig.oracle_files == ("src/x.py", "src/y.py")

    def test_read_sdlc_weighted_checklist(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "t"
        (task_dir / "tests").mkdir(parents=True)
        (task_dir / "tests" / "ground_truth.json").write_text(
            json.dumps(
                {
                    "schema_version": "sdlc-v1",
                    "source_files": ["src/foo.py", "src/bar.py"],
                }
            )
        )
        (task_dir / "metadata.json").write_text(
            json.dumps({"id": "t", "metadata": {}, "verification": {}})
        )
        sig = read_structural_signature(task_dir)
        assert sig.oracle_type == "weighted_checklist"
        assert sig.oracle_files == ("src/bar.py", "src/foo.py")

    def test_read_missing_ground_truth(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "t"
        task_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            read_structural_signature(task_dir)


# ---------------------------------------------------------------------------
# signature_from_task
# ---------------------------------------------------------------------------


class TestSignatureFromTask:
    def test_signature_from_oracle_task(self) -> None:
        task = _oracle_task(files=("a", "b"))
        sig = signature_from_task(task)
        assert sig.oracle_type == "file_list"
        assert sig.oracle_files == ("a", "b")

    def test_signature_from_legacy_dual_task_without_oracle_type(self) -> None:
        """Legacy dual tasks lack ``oracle_type`` but still carry a file list.

        Regression guard: the second branch in :func:`signature_from_task`
        used to be an unreachable ``elif`` because it repeated the
        oracle_type check. A legacy dual task with empty ``oracle_type``
        must still classify as ``file_list`` so refresh-time diffs work.
        """
        task = Task(
            id="legacy-dual",
            repo="example/repo",
            metadata=TaskMetadata(
                name="legacy-dual",
                description="legacy dual task",
                task_type="dual",
            ),
            verification=TaskVerification(
                type="dual",
                verification_mode="dual",
                oracle_type="",  # legacy: no explicit oracle_type
                oracle_answer=("src/legacy_a.py", "src/legacy_b.py"),
            ),
        )
        sig = signature_from_task(task)
        assert sig.oracle_type == "file_list"
        assert sig.oracle_files == ("src/legacy_a.py", "src/legacy_b.py")


# ---------------------------------------------------------------------------
# refresh_task
# ---------------------------------------------------------------------------


class TestRefreshTask:
    def test_preserves_task_id_when_structurally_identical(
        self, tmp_path: Path
    ) -> None:
        old = _oracle_task(
            task_id="tsk-abc",
            files=("src/a.py", "src/b.py", "src/c.py"),
            commit="c0",
        )
        task_dir = _write_existing_task_dir(tmp_path, old)

        # New task has same files but a different synthetic ID; refresh
        # must override to the preserved ID.
        new = _oracle_task(
            task_id="SHOULD-BE-OVERRIDDEN",
            files=("src/a.py", "src/b.py", "src/c.py"),
            commit="c1",
        )
        result = refresh_task(task_dir, new, new_commit="c1")

        assert result.preserved_id
        assert not result.renumbered
        assert result.task.id == "tsk-abc"
        assert result.task.metadata.ground_truth_commit == "c1"
        # History must be [oldest -> newest].
        assert result.task.metadata.ground_truth_commit_history == ("c0", "c1")

    def test_fails_loud_on_oracle_type_change(self, tmp_path: Path) -> None:
        old = _oracle_task(oracle_type="file_list", commit="c0")
        task_dir = _write_existing_task_dir(tmp_path, old)

        new = _oracle_task(oracle_type="count", commit="c1")

        with pytest.raises(StructuralMismatchError) as exc:
            refresh_task(task_dir, new, new_commit="c1")
        assert exc.value.diff.oracle_type_changed
        # Diff report is informative.
        report = str(exc.value)
        assert "oracle_type" in report
        assert "file_list" in report
        assert "count" in report
        # Remediation must name the exact CLI invocation, not just the flag.
        assert "codeprobe mine --refresh" in report
        assert "--accept-structural-change" in report
        assert str(task_dir) in report

    def test_fails_loud_on_file_churn_above_threshold(
        self, tmp_path: Path
    ) -> None:
        # 5 files → replace 3 → churn ≈ 0.75 ≫ 0.20.
        old = _oracle_task(files=("a", "b", "c", "d", "e"), commit="c0")
        task_dir = _write_existing_task_dir(tmp_path, old)

        new = _oracle_task(files=("a", "b", "x", "y", "z"), commit="c1")

        with pytest.raises(StructuralMismatchError) as exc:
            refresh_task(task_dir, new, new_commit="c1")
        assert exc.value.diff.churn > CHURN_THRESHOLD
        assert "x" in set(exc.value.diff.added_files)
        assert "d" in set(exc.value.diff.removed_files)

    def test_accept_structural_change_allows_refresh(
        self, tmp_path: Path
    ) -> None:
        old = _oracle_task(
            task_id="tsk-keeper",
            files=("a", "b", "c"),
            commit="c0",
        )
        task_dir = _write_existing_task_dir(tmp_path, old)

        new = _oracle_task(files=("x", "y", "z"), commit="c1")
        result = refresh_task(
            task_dir, new, new_commit="c1", accept_structural_change=True
        )

        assert result.renumbered
        assert result.task.id == "tsk-keeper"  # ID preserved, history reset
        assert result.task.metadata.ground_truth_commit_history == ("c1",)

    def test_ground_truth_commit_history_order(self, tmp_path: Path) -> None:
        """Verify history grows chronologically across successive refreshes."""
        task = _oracle_task(task_id="tsk-seq", files=("a", "b"), commit="c1")
        task_dir = _write_existing_task_dir(tmp_path, task)

        # First refresh: c1 → c2.
        new2 = _oracle_task(files=("a", "b"), commit="c2")
        r2 = refresh_task(task_dir, new2, new_commit="c2")
        assert r2.task.metadata.ground_truth_commit_history == ("c1", "c2")

        # Persist r2 back so the next read sees the extended history.
        write_task_dir(r2.task, task_dir.parent, tmp_path / "repo")

        # Second refresh: c2 → c3.
        new3 = _oracle_task(files=("a", "b"), commit="c3")
        r3 = refresh_task(task_dir, new3, new_commit="c3")
        assert r3.task.metadata.ground_truth_commit_history == ("c1", "c2", "c3")

    def test_no_silent_renumbering(self, tmp_path: Path) -> None:
        """Structural mismatch without the flag must not overwrite the task."""
        old = _oracle_task(task_id="tsk-stable", files=("a", "b", "c"))
        task_dir = _write_existing_task_dir(tmp_path, old)

        # Snapshot the on-disk metadata.json before the failed refresh.
        md_path = task_dir / "metadata.json"
        before = md_path.read_text(encoding="utf-8")

        new = _oracle_task(task_id="NEW-ID", files=("x", "y", "z"), commit="c1")
        with pytest.raises(StructuralMismatchError):
            refresh_task(task_dir, new, new_commit="c1")

        # refresh_task is pure — it must not have touched the filesystem.
        after = md_path.read_text(encoding="utf-8")
        assert before == after

        # And the task ID on disk is still the original.
        loaded = json.loads(after)
        assert loaded["id"] == "tsk-stable"

    def test_refresh_sets_commit_on_new_task(self, tmp_path: Path) -> None:
        old = _oracle_task(files=("a",), commit="c0")
        task_dir = _write_existing_task_dir(tmp_path, old)

        new = _oracle_task(files=("a",), commit="ignored-because-overridden")
        result = refresh_task(task_dir, new, new_commit="deadbeef")
        assert result.task.metadata.ground_truth_commit == "deadbeef"

    def test_missing_metadata_raises(self, tmp_path: Path) -> None:
        task_dir = tmp_path / "orphan"
        (task_dir / "tests").mkdir(parents=True)
        (task_dir / "tests" / "ground_truth.json").write_text(
            json.dumps({"oracle_type": "file_list", "expected": ["a"]})
        )
        new = _oracle_task(files=("a",))
        with pytest.raises(FileNotFoundError):
            refresh_task(task_dir, new, new_commit="c1")

    def test_seeds_history_from_single_commit_field(self, tmp_path: Path) -> None:
        """A task mined before R20 has ground_truth_commit but no history.

        The refresh should seed the history from the old single-commit
        field so the resulting chain is [old_commit, new_commit].
        """
        old = _oracle_task(files=("a",), commit="pre-r20", history=())
        task_dir = _write_existing_task_dir(tmp_path, old)

        new = _oracle_task(files=("a",), commit="post-r20")
        result = refresh_task(task_dir, new, new_commit="post-r20")
        assert result.task.metadata.ground_truth_commit_history == (
            "pre-r20",
            "post-r20",
        )

    def test_task_metadata_ground_truth_commit_history_field_exists(self) -> None:
        """AC4: TaskMetadata carries a list-like history field."""
        md = TaskMetadata(name="t", ground_truth_commit_history=("a", "b", "c"))
        d = asdict(md)
        assert d["ground_truth_commit_history"] == ("a", "b", "c")
