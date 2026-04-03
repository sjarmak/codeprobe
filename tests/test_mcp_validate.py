"""Tests for MCP delta validation module."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeprobe.mining.org_scale_families import TaskFamily
from codeprobe.mining.org_scale_validate import (
    DeltaResult,
    validate_families,
    validate_family_delta,
)
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TEST_FAMILY = TaskFamily(
    name="test-deprecated",
    description="Find files with @Deprecated annotations.",
    glob_patterns=("**/*.py",),
    content_patterns=(r"@[Dd]eprecated",),
    min_hits=1,
    max_hits=100,
    multi_hop=True,
    multi_hop_description="Find callers of deprecated symbols.",
)


def _make_task(task_id: str, oracle_answer: tuple[str, ...]) -> Task:
    return Task(
        id=task_id,
        repo="test/repo",
        metadata=TaskMetadata(name=task_id),
        verification=TaskVerification(
            oracle_type="file_list",
            oracle_answer=oracle_answer,
        ),
    )


def _init_git_repo(repo_path: Path) -> None:
    """Initialize a git repo and commit all files."""
    subprocess.run(["git", "init"], cwd=str(repo_path), capture_output=True, check=True)
    subprocess.run(
        ["git", "add", "-A"], cwd=str(repo_path), capture_output=True, check=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init", "--allow-empty"],
        cwd=str(repo_path),
        capture_output=True,
        check=True,
        env={
            "GIT_AUTHOR_NAME": "test",
            "GIT_AUTHOR_EMAIL": "test@test.com",
            "GIT_COMMITTER_NAME": "test",
            "GIT_COMMITTER_EMAIL": "test@test.com",
            "HOME": str(repo_path),
            "PATH": "/usr/bin:/bin",
        },
    )


# ---------------------------------------------------------------------------
# Integration tests: validate_family_delta
# ---------------------------------------------------------------------------


class TestValidateFamilyDelta:
    def test_high_overlap_is_baseline_only(self, tmp_path: Path) -> None:
        """When grep finds all ground truth files, family is baseline_only."""
        repo = tmp_path / "repo"
        repo.mkdir()

        # Create files that ALL match the deprecated pattern
        for name in ("alpha.py", "beta.py", "gamma.py"):
            (repo / name).write_text(f"# @Deprecated\ndef old_{name}(): pass\n")

        _init_git_repo(repo)

        # Ground truth = exactly the files grep will find
        task = _make_task("t1", ("alpha.py", "beta.py", "gamma.py"))
        result = validate_family_delta(_TEST_FAMILY, [task], [repo])

        assert result.family_name == "test-deprecated"
        assert result.grep_f1 >= 0.95
        assert result.is_baseline_only is True
        assert result.sample_count == 1

    def test_low_overlap_not_baseline_only(self, tmp_path: Path) -> None:
        """When ground truth has files grep can't find, family is NOT baseline_only."""
        repo = tmp_path / "repo"
        repo.mkdir()

        # Only one file matches the deprecated pattern
        (repo / "deprecated.py").write_text("# @Deprecated\ndef old(): pass\n")
        # These files exist but don't match the pattern (multi-hop callers)
        (repo / "caller_a.py").write_text("from deprecated import old\nold()\n")
        (repo / "caller_b.py").write_text("from deprecated import old\nold()\n")
        (repo / "caller_c.py").write_text("from deprecated import old\nold()\n")

        _init_git_repo(repo)

        # Ground truth includes callers that grep won't find
        task = _make_task(
            "t2",
            ("deprecated.py", "caller_a.py", "caller_b.py", "caller_c.py"),
        )
        result = validate_family_delta(_TEST_FAMILY, [task], [repo])

        assert result.family_name == "test-deprecated"
        assert result.grep_f1 < 0.95
        assert result.is_baseline_only is False
        assert result.sample_count == 1

    def test_empty_tasks_graceful(self) -> None:
        """Empty task list returns sample_count=0 and is_baseline_only=False."""
        result = validate_family_delta(_TEST_FAMILY, [], [])

        assert result.family_name == "test-deprecated"
        assert result.grep_f1 == 0.0
        assert result.is_baseline_only is False
        assert result.sample_count == 0
        assert "No sample tasks" in result.details

    def test_task_with_empty_ground_truth(self, tmp_path: Path) -> None:
        """Task with empty oracle_answer is skipped gracefully."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "foo.py").write_text("# @Deprecated\ndef x(): pass\n")
        _init_git_repo(repo)

        task = _make_task("t3", ())
        result = validate_family_delta(_TEST_FAMILY, [task], [repo])

        # Skipped task means no f1 scores collected → avg is 0
        assert result.sample_count == 0
        assert result.is_baseline_only is False


# ---------------------------------------------------------------------------
# Integration test: validate_families
# ---------------------------------------------------------------------------


class TestValidateFamilies:
    def test_multiple_families(self, tmp_path: Path) -> None:
        """Validates multiple families in one call."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("# @Deprecated\ndef old(): pass\n")
        _init_git_repo(repo)

        family_a = _TEST_FAMILY
        family_b = TaskFamily(
            name="test-errors",
            description="Find error types.",
            glob_patterns=("**/*.py",),
            content_patterns=(r"class\s+\w+Error",),
            min_hits=1,
        )

        task_a = _make_task("ta", ("a.py",))
        task_b = _make_task("tb", ("a.py", "missing.py"))

        results = validate_families(
            [family_a, family_b],
            [[task_a], [task_b]],
            [[repo], [repo]],
        )

        assert len(results) == 2
        assert results[0].family_name == "test-deprecated"
        assert results[1].family_name == "test-errors"
        assert all(isinstance(r, DeltaResult) for r in results)
