"""Tests for the task mining module."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from codeprobe.mining.extractor import (
    extract_task_from_merge,
    list_merged_prs,
    mine_tasks,
)
from codeprobe.mining.sources import RepoSource, detect_source
from codeprobe.mining.writer import write_task_dir
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

# ---------------------------------------------------------------------------
# detect_source tests
# ---------------------------------------------------------------------------


def _mock_git_remote(url: str, returncode: int = 0):
    """Return a mock for subprocess.run that simulates git remote get-url."""

    def _side_effect(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=returncode,
            stdout=url if returncode == 0 else "",
            stderr="" if returncode == 0 else "fatal: No such remote 'origin'",
        )

    return _side_effect


class TestDetectSource:
    @patch("codeprobe.mining.sources.subprocess.run")
    def test_detect_source_github_https(self, mock_run: object) -> None:
        mock_run.side_effect = _mock_git_remote("https://github.com/owner/repo.git\n")
        source = detect_source(Path("/fake/repo"))

        assert source.host == "github"
        assert source.owner == "owner"
        assert source.repo == "repo"
        assert "github.com" in source.remote_url

    @patch("codeprobe.mining.sources.subprocess.run")
    def test_detect_source_github_ssh(self, mock_run: object) -> None:
        mock_run.side_effect = _mock_git_remote("git@github.com:owner/repo.git\n")
        source = detect_source(Path("/fake/repo"))

        assert source.host == "github"
        assert source.owner == "owner"
        assert source.repo == "repo"

    @patch("codeprobe.mining.sources.subprocess.run")
    def test_detect_source_gitlab(self, mock_run: object) -> None:
        mock_run.side_effect = _mock_git_remote("https://gitlab.com/myorg/mylib.git\n")
        source = detect_source(Path("/fake/repo"))

        assert source.host == "gitlab"
        assert source.owner == "myorg"
        assert source.repo == "mylib"

    @patch("codeprobe.mining.sources.subprocess.run")
    def test_detect_source_bitbucket(self, mock_run: object) -> None:
        mock_run.side_effect = _mock_git_remote("git@bitbucket.org:team/project.git\n")
        source = detect_source(Path("/fake/repo"))

        assert source.host == "bitbucket"
        assert source.owner == "team"
        assert source.repo == "project"

    @patch("codeprobe.mining.sources.subprocess.run")
    def test_detect_source_azure(self, mock_run: object) -> None:
        mock_run.side_effect = _mock_git_remote(
            "https://dev.azure.com/myorg/myproject/_git/myrepo\n"
        )
        source = detect_source(Path("/fake/repo"))

        assert source.host == "azure"
        assert source.owner == "myorg"
        assert source.repo == "myrepo"

    @patch("codeprobe.mining.sources.subprocess.run")
    def test_detect_source_gitea(self, mock_run: object) -> None:
        mock_run.side_effect = _mock_git_remote("https://gitea.example.com/user/repo.git\n")
        source = detect_source(Path("/fake/repo"))

        assert source.host == "self-hosted"
        assert source.owner == "user"
        assert source.repo == "repo"

    @patch("codeprobe.mining.sources.subprocess.run")
    def test_detect_source_local_no_remote(self, mock_run: object) -> None:
        mock_run.side_effect = _mock_git_remote("", returncode=1)
        source = detect_source(Path("/fake/my-project"))

        assert source.host == "local"
        assert source.owner == ""
        assert source.repo == "my-project"
        assert source.remote_url == ""

    @patch("codeprobe.mining.sources.subprocess.run")
    def test_detect_source_timeout(self, mock_run: object) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=10)
        source = detect_source(Path("/fake/timed-out"))

        assert source.host == "local"
        assert source.repo == "timed-out"


# ---------------------------------------------------------------------------
# list_merged_prs tests
# ---------------------------------------------------------------------------


def _mock_git_log(output: str, returncode: int = 0):
    """Return a mock side_effect for subprocess.run simulating git log."""

    def _side_effect(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=returncode,
            stdout=output,
            stderr="",
        )

    return _side_effect


class TestListMergedPRs:
    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_list_merged_prs(self, mock_run: object) -> None:
        log_output = (
            "abc12345deadbeef Merge pull request #10 from feature/auth\n"
            "def67890cafebabe Merge pull request #9 from fix/login-bug\n"
        )
        mock_run.side_effect = _mock_git_log(log_output)
        source = RepoSource(host="github", owner="o", repo="r", remote_url="")

        prs = list_merged_prs(source, Path("/fake"), limit=10)

        assert len(prs) == 2
        assert prs[0].sha == "abc12345deadbeef"
        assert "auth" in prs[0].title
        assert prs[1].sha == "def67890cafebabe"

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_list_merged_prs_empty(self, mock_run: object) -> None:
        mock_run.side_effect = _mock_git_log("")
        source = RepoSource(host="local", owner="", repo="r", remote_url="")

        prs = list_merged_prs(source, Path("/fake"), limit=5)
        assert prs == []


# ---------------------------------------------------------------------------
# extract_task_from_merge tests
# ---------------------------------------------------------------------------


def _mock_diff_name_only(files: list[str]):
    """Mock subprocess.run to return a list of changed file names."""

    def _side_effect(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout="\n".join(files) + "\n" if files else "",
            stderr="",
        )

    return _side_effect


class TestExtractTaskFromMerge:
    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_extract_with_tests(self, mock_run: object) -> None:
        files = ["src/auth.py", "tests/test_auth.py", "README.md"]
        mock_run.side_effect = _mock_diff_name_only(files)

        task = extract_task_from_merge("abc12345deadbeef", Path("/fake/myrepo"))

        assert task is not None
        assert task.id == "abc12345"
        assert task.repo == "myrepo"
        assert task.metadata.difficulty == "easy"
        assert task.metadata.language == "python"

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_extract_no_tests_filtered(self, mock_run: object) -> None:
        files = ["src/auth.py", "src/models.py", "README.md"]
        mock_run.side_effect = _mock_diff_name_only(files)

        task = extract_task_from_merge("abc12345deadbeef", Path("/fake/myrepo"))
        assert task is None

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_extract_medium_difficulty(self, mock_run: object) -> None:
        files = [f"src/file{i}.py" for i in range(5)] + ["tests/test_all.py"]
        mock_run.side_effect = _mock_diff_name_only(files)

        task = extract_task_from_merge("deadbeefcafebabe", Path("/fake/repo"))

        assert task is not None
        assert task.metadata.difficulty == "medium"

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_extract_hard_difficulty(self, mock_run: object) -> None:
        files = [f"src/file{i}.py" for i in range(12)] + ["tests/test_big.py"]
        mock_run.side_effect = _mock_diff_name_only(files)

        task = extract_task_from_merge("1111222233334444", Path("/fake/repo"))

        assert task is not None
        assert task.metadata.difficulty == "hard"


# ---------------------------------------------------------------------------
# mine_tasks tests
# ---------------------------------------------------------------------------


class TestMineTasks:
    @patch("codeprobe.mining.extractor.subprocess.run")
    @patch("codeprobe.mining.sources.subprocess.run")
    def test_mine_tasks_from_merge_commits(self, mock_sources_run: object, mock_extractor_run: object) -> None:
        merge_log = (
            "aaaa1111bbbb2222 Merge PR #1 feature\n"
            "cccc3333dddd4444 Merge PR #2 bugfix\n"
        )
        diff_files_1 = "src/feat.py\ntests/test_feat.py\n"
        diff_files_2 = "src/bug.py\ntests/test_bug.py\n"

        # sources module only handles "git remote"
        mock_sources_run.side_effect = _mock_git_remote("https://github.com/o/r.git\n")

        # extractor module handles "git log" and "git diff"
        diff_call = {"n": 0}

        def _extractor_side_effect(cmd, **kwargs):
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 0, merge_log, "")
            diff_call["n"] += 1
            if diff_call["n"] == 1:
                return subprocess.CompletedProcess(cmd, 0, diff_files_1, "")
            return subprocess.CompletedProcess(cmd, 0, diff_files_2, "")

        mock_extractor_run.side_effect = _extractor_side_effect

        tasks = mine_tasks(Path("/fake/repo"), count=5)

        assert len(tasks) == 2
        assert all(isinstance(t, Task) for t in tasks)

    @patch("codeprobe.mining.extractor.subprocess.run")
    @patch("codeprobe.mining.sources.subprocess.run")
    def test_mine_tasks_no_tests_filtered(self, mock_sources_run: object, mock_extractor_run: object) -> None:
        merge_log = "aaaa1111bbbb2222 Merge PR #1\n"
        diff_files = "src/auth.py\nREADME.md\n"

        mock_sources_run.side_effect = _mock_git_remote("https://github.com/o/r.git\n")

        def _extractor_side_effect(cmd, **kwargs):
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 0, merge_log, "")
            return subprocess.CompletedProcess(cmd, 0, diff_files, "")

        mock_extractor_run.side_effect = _extractor_side_effect

        tasks = mine_tasks(Path("/fake/repo"), count=5)
        assert tasks == []

    @patch("codeprobe.mining.extractor.subprocess.run")
    @patch("codeprobe.mining.sources.subprocess.run")
    def test_mine_tasks_respects_count(self, mock_sources_run: object, mock_extractor_run: object) -> None:
        # Generate 10 merge commits with distinct 8-char prefixes
        lines = [f"{(i + 1) * 0x11111111:016x} Merge PR #{i}" for i in range(10)]
        merge_log = "\n".join(lines) + "\n"
        diff_files = "src/code.py\ntests/test_code.py\n"

        mock_sources_run.side_effect = _mock_git_remote("https://github.com/o/r.git\n")

        def _extractor_side_effect(cmd, **kwargs):
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 0, merge_log, "")
            return subprocess.CompletedProcess(cmd, 0, diff_files, "")

        mock_extractor_run.side_effect = _extractor_side_effect

        tasks = mine_tasks(Path("/fake/repo"), count=3)
        assert len(tasks) == 3


# ---------------------------------------------------------------------------
# write_task_dir tests
# ---------------------------------------------------------------------------


class TestWriteTaskDir:
    def test_write_task_dir(self, tmp_path: Path) -> None:
        task = Task(
            id="abc12345",
            repo="myrepo",
            metadata=TaskMetadata(
                name="merge-abc12345",
                difficulty="easy",
                description="Reproduce changes from merge commit abc12345",
                language="python",
            ),
            verification=TaskVerification(
                type="test_script",
                command="bash tests/test.sh",
                reward_type="binary",
            ),
        )

        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)

        assert result_path == base_dir / "abc12345"
        assert result_path.is_dir()

        # Check instruction.md
        instruction = (result_path / "instruction.md").read_text(encoding="utf-8")
        assert "merge-abc12345" in instruction
        assert "Reproduce the changes" in instruction

        # Check tests/test.sh
        test_sh = result_path / "tests" / "test.sh"
        assert test_sh.is_file()
        content = test_sh.read_text(encoding="utf-8")
        assert "#!/usr/bin/env bash" in content
        assert "set -euo pipefail" in content
        # Verify it's executable
        assert test_sh.stat().st_mode & 0o755

        # Check metadata.json
        meta_path = result_path / "metadata.json"
        assert meta_path.is_file()
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["id"] == "abc12345"
        assert meta["repo"] == "myrepo"
        assert meta["metadata"]["difficulty"] == "easy"

    def test_write_task_dir_creates_parents(self, tmp_path: Path) -> None:
        task = Task(
            id="deadbeef",
            repo="r",
            metadata=TaskMetadata(name="merge-deadbeef"),
        )
        nested = tmp_path / "deep" / "nested" / "tasks"
        result = write_task_dir(task, nested, tmp_path)
        assert result.is_dir()
        assert (result / "instruction.md").is_file()
