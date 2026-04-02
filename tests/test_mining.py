"""Tests for the task mining module."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from codeprobe.mining.extractor import (
    MergedPR,
    PRMetadata,
    _build_test_command,
    _extract_issue_numbers,
    _format_task_description,
    enrich_task,
    enrich_tasks,
    extract_subsystems,
    extract_task_from_merge,
    list_merged_prs,
    mine_tasks,
    resolve_pr_metadata,
    score_pr_quality,
)
from codeprobe.mining.sources import RepoSource, detect_source
from codeprobe.mining.writer import write_task_dir
from codeprobe.models.task import Task, TaskMetadata, TaskVerification

# ---------------------------------------------------------------------------
# detect_source tests
# ---------------------------------------------------------------------------


def _mock_subprocess(stdout: str = "", returncode: int = 0, stderr: str = ""):
    """Return a mock side_effect for subprocess.run with fixed output."""

    def _side_effect(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    return _side_effect


def _mock_git_remote(url: str, returncode: int = 0):
    """Return a mock for subprocess.run that simulates git remote get-url."""
    if returncode != 0:
        return _mock_subprocess(
            returncode=returncode, stderr="fatal: No such remote 'origin'"
        )
    return _mock_subprocess(stdout=url)


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
        mock_run.side_effect = _mock_git_remote(
            "https://gitea.example.com/user/repo.git\n"
        )
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
    return _mock_subprocess(stdout=output, returncode=returncode)


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
    stdout = "\n".join(files) + "\n" if files else ""
    return _mock_subprocess(stdout=stdout)


def _mock_with_commit_body(files: list[str], commit_body: str):
    """Mock subprocess.run for both git diff (file list) and git log (commit body)."""
    file_stdout = "\n".join(files) + "\n" if files else ""

    def _side_effect(cmd, **kwargs):
        if "log" in cmd:
            return subprocess.CompletedProcess(cmd, 0, commit_body, "")
        return subprocess.CompletedProcess(cmd, 0, file_stdout, "")

    return _side_effect


class TestExtractTaskFromMerge:
    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_extract_with_enriched_source(self, mock_run: object) -> None:
        files = ["src/auth.py", "tests/test_auth.py", "README.md"]
        mock_run.side_effect = _mock_with_commit_body(
            files, "Fix auth\n\nTokens expired silently on 401."
        )
        source = RepoSource(host="local", owner="", repo="myrepo", remote_url="")

        result = extract_task_from_merge(
            "abc12345deadbeef",
            Path("/fake/myrepo"),
            source=source,
            merge_title="Fix auth",
        )

        assert result is not None
        task, pr_meta = result
        assert task.id == "abc12345"
        assert task.repo == "myrepo"
        assert task.metadata.difficulty == "easy"
        assert task.metadata.language == "python"
        assert pr_meta.source_tier == "commit_message"

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_extract_bare_metadata_filtered(self, mock_run: object) -> None:
        """Tasks with no PR body or commit message body are rejected."""
        files = ["src/auth.py", "tests/test_auth.py", "README.md"]
        mock_run.side_effect = _mock_diff_name_only(files)

        # No source → bare metadata → filtered
        result = extract_task_from_merge("abc12345deadbeef", Path("/fake/myrepo"))
        assert result is None

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_extract_stub_test_command_filtered(self, mock_run: object) -> None:
        """Tasks with unsupported languages get stub test commands and are rejected."""
        # Rust files — unsupported language → _DEFAULT_TEST_COMMAND
        files = ["src/auth.rs", "tests/test_auth.rs"]
        mock_run.side_effect = _mock_with_commit_body(
            files, "Fix auth\n\nTokens expired silently."
        )
        source = RepoSource(host="local", owner="", repo="r", remote_url="")

        result = extract_task_from_merge(
            "abc12345deadbeef", Path("/fake/r"), source=source, merge_title="Fix auth"
        )
        assert result is None

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_extract_no_tests_filtered(self, mock_run: object) -> None:
        files = ["src/auth.py", "src/models.py", "README.md"]
        mock_run.side_effect = _mock_diff_name_only(files)

        result = extract_task_from_merge("abc12345deadbeef", Path("/fake/myrepo"))
        assert result is None

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_extract_medium_difficulty(self, mock_run: object) -> None:
        files = [f"src/file{i}.py" for i in range(5)] + ["tests/test_all.py"]
        mock_run.side_effect = _mock_with_commit_body(
            files, "Big refactor\n\nRestructured modules for clarity."
        )
        source = RepoSource(host="local", owner="", repo="repo", remote_url="")

        result = extract_task_from_merge(
            "deadbeefcafebabe",
            Path("/fake/repo"),
            source=source,
            merge_title="Big refactor",
        )

        assert result is not None
        task, _ = result
        assert task.metadata.difficulty == "medium"

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_extract_hard_difficulty(self, mock_run: object) -> None:
        files = [f"src/file{i}.py" for i in range(12)] + ["tests/test_big.py"]
        mock_run.side_effect = _mock_with_commit_body(
            files, "Major feature\n\nAdded distributed caching layer."
        )
        source = RepoSource(host="local", owner="", repo="repo", remote_url="")

        result = extract_task_from_merge(
            "1111222233334444",
            Path("/fake/repo"),
            source=source,
            merge_title="Major feature",
        )

        assert result is not None
        task, _ = result
        assert task.metadata.difficulty == "hard"


# ---------------------------------------------------------------------------
# mine_tasks tests
# ---------------------------------------------------------------------------


class TestMineTasks:
    @patch("codeprobe.mining.extractor.subprocess.run")
    @patch("codeprobe.mining.sources.subprocess.run")
    def test_mine_tasks_from_merge_commits(
        self, mock_sources_run: object, mock_extractor_run: object
    ) -> None:
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
    def test_mine_tasks_no_tests_filtered(
        self, mock_sources_run: object, mock_extractor_run: object
    ) -> None:
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
    def test_mine_tasks_filters_low_quality(
        self, mock_sources_run: object, mock_extractor_run: object
    ) -> None:
        """Tasks below the min_quality threshold are excluded from results."""
        # Two merges: one with PR context (#1 ref + body), one bare/generic
        merge_log = (
            "aaaa1111bbbb2222 Merge pull request #1 from fix/auth\n"
            "cccc3333dddd4444 Squash merge of cleanup branch\n"
        )
        good_files = "src/auth.py\ntests/test_auth.py\n"
        bare_files = "src/misc.py\ntests/test_other.py\n"

        mock_sources_run.side_effect = _mock_git_remote("https://github.com/o/r.git\n")

        diff_call = {"n": 0}

        def _extractor_side_effect(cmd, **kwargs):
            if "log" in cmd:
                # For merge log listing, return the log
                if "--merges" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, merge_log, "")
                # For commit body: first commit has body, second is bare
                if "aaaa1111" in str(cmd):
                    return subprocess.CompletedProcess(
                        cmd, 0, "Fix auth\n\nTokens expired on 401.", ""
                    )
                # Bare commit — empty body
                return subprocess.CompletedProcess(cmd, 1, "", "")
            if "diff" in cmd:
                diff_call["n"] += 1
                if diff_call["n"] == 1:
                    return subprocess.CompletedProcess(cmd, 0, good_files, "")
                return subprocess.CompletedProcess(cmd, 0, bare_files, "")
            # gh pr view
            if cmd[0] == "gh":
                return subprocess.CompletedProcess(cmd, 1, "", "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        mock_extractor_run.side_effect = _extractor_side_effect

        # Default min_quality filters out the bare task
        tasks = mine_tasks(Path("/fake/repo"), count=5)
        assert len(tasks) == 1
        assert tasks[0].id == "aaaa1111"

    @patch("codeprobe.mining.extractor.subprocess.run")
    @patch("codeprobe.mining.sources.subprocess.run")
    def test_mine_tasks_bare_metadata_hard_gated(
        self, mock_sources_run: object, mock_extractor_run: object
    ) -> None:
        """Tasks with bare metadata are rejected even with min_quality=0."""
        merge_log = "aaaa1111bbbb2222 Squash merge\n"
        diff_files = "src/code.py\ntests/test_code.py\n"

        mock_sources_run.side_effect = _mock_git_remote("https://github.com/o/r.git\n")

        def _extractor_side_effect(cmd, **kwargs):
            if "log" in cmd:
                if "--merges" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, merge_log, "")
                # No commit body → bare metadata
                return subprocess.CompletedProcess(cmd, 1, "", "")
            if "diff" in cmd:
                return subprocess.CompletedProcess(cmd, 0, diff_files, "")
            return subprocess.CompletedProcess(cmd, 1, "", "")

        mock_extractor_run.side_effect = _extractor_side_effect

        # Even min_quality=0 can't bypass hard gates
        tasks = mine_tasks(Path("/fake/repo"), count=5, min_quality=0.0)
        assert tasks == []

    @patch("codeprobe.mining.extractor.subprocess.run")
    @patch("codeprobe.mining.sources.subprocess.run")
    def test_mine_tasks_respects_count(
        self, mock_sources_run: object, mock_extractor_run: object
    ) -> None:
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
        assert "Implement the changes" in instruction

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

    def test_write_enriched_instruction(self, tmp_path: Path) -> None:
        """Enriched multi-line description appears in instruction.md (fallback path)."""
        task = Task(
            id="enrich01",
            repo="myrepo",
            metadata=TaskMetadata(
                name="merge-enrich01",
                difficulty="easy",
                description="Fix auth token refresh\n\nTokens were not refreshed on 401.\nThis caused silent logout.\n\nLabels: bug, auth",
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

        instruction = (result_path / "instruction.md").read_text(encoding="utf-8")
        assert "merge-enrich01" in instruction
        assert "Tokens were not refreshed" in instruction
        assert "Implement the changes" in instruction


# ---------------------------------------------------------------------------
# _build_test_command tests
# ---------------------------------------------------------------------------


import pytest


class TestBuildTestCommand:
    @pytest.mark.parametrize(
        "language,test_files,expected",
        [
            pytest.param(
                "python",
                ["tests/test_auth.py", "tests/test_login.py"],
                "pytest tests/test_auth.py tests/test_login.py",
                id="python-pytest",
            ),
            pytest.param(
                "go",
                ["pkg/auth/auth_test.go"],
                "go test ./pkg/auth/...",
                id="go-package",
            ),
            pytest.param(
                "go",
                ["internal/cache/cache_test.go", "internal/cache/lru_test.go"],
                "go test ./internal/cache/...",
                id="go-deduped-package",
            ),
            pytest.param(
                "typescript",
                ["src/__tests__/auth.test.ts"],
                "npm test -- --testPathPattern=auth.test.ts",
                id="typescript-jest",
            ),
            pytest.param(
                "javascript",
                ["test/api.spec.js"],
                "npm test -- --testPathPattern=api.spec.js",
                id="javascript-jest",
            ),
            pytest.param(
                "rust",
                ["src/auth/mod_test.rs"],
                "bash tests/test.sh",
                id="unsupported-language-fallback",
            ),
            pytest.param(
                "",
                ["tests/test_foo.py"],
                "bash tests/test.sh",
                id="empty-language-fallback",
            ),
        ],
    )
    def test_build_test_command(
        self, language: str, test_files: list[str], expected: str
    ) -> None:
        result = _build_test_command(language, test_files)
        assert result == expected

    def test_empty_test_files_fallback(self) -> None:
        result = _build_test_command("python", [])
        assert result == "bash tests/test.sh"


# ---------------------------------------------------------------------------
# score_pr_quality tests
# ---------------------------------------------------------------------------


class TestScorePRQuality:
    def test_perfect_pr(self) -> None:
        """PR with issue ref, description, targeted tests, and linked issue scores 1.0."""
        score = score_pr_quality(
            title="Fix auth redirect",
            body="Fixes #42. Tokens were not refreshed on 401 responses. This caused silent logout.",
            changed_files=["src/auth.py", "src/middleware.py", "tests/test_auth.py"],
            test_files=["tests/test_auth.py"],
            has_linked_issue=True,
        )
        assert score == pytest.approx(1.0)

    def test_three_signals_without_linked_issue(self) -> None:
        """PR with issue ref, description, and targeted tests but no linked issue scores 3/4."""
        score = score_pr_quality(
            title="Fix auth redirect",
            body="Fixes #42. Tokens were not refreshed on 401 responses. This caused silent logout.",
            changed_files=["src/auth.py", "src/middleware.py", "tests/test_auth.py"],
            test_files=["tests/test_auth.py"],
        )
        assert score == pytest.approx(3 / 4, abs=0.01)

    def test_no_issue_ref(self) -> None:
        """PR without issue reference in body loses that signal."""
        score = score_pr_quality(
            title="Fix auth redirect",
            body="Tokens were not refreshed on 401 responses.",
            changed_files=["src/auth.py", "tests/test_auth.py"],
            test_files=["tests/test_auth.py"],
        )
        assert score == pytest.approx(2 / 4, abs=0.01)

    def test_issue_ref_in_title_only_not_counted(self) -> None:
        """Issue ref only in title is noise (GitHub merge titles always have #N)."""
        score = score_pr_quality(
            title="Fix auth redirect (#42)",
            body="",
            changed_files=["src/auth.py", "tests/test_auth.py"],
            test_files=["tests/test_auth.py"],
        )
        # Only test overlap signal — title #N is ignored, body is empty
        assert score == pytest.approx(1 / 4, abs=0.01)

    def test_empty_body(self) -> None:
        """PR with empty body loses body and issue ref signals."""
        score = score_pr_quality(
            title="Fix auth redirect",
            body="",
            changed_files=["src/auth.py", "tests/test_auth.py"],
            test_files=["tests/test_auth.py"],
        )
        # Only test overlap signal
        assert score == pytest.approx(1 / 4, abs=0.01)

    def test_unrelated_tests(self) -> None:
        """Test files that don't share source stems get no test coverage signal."""
        score = score_pr_quality(
            title="Fix auth redirect",
            body="Fixes #42. Good description of the problem and fix.",
            changed_files=["src/auth.py", "tests/test_unrelated.py"],
            test_files=["tests/test_unrelated.py"],
        )
        # Issue ref in body + body present, but no test/source overlap
        assert score == pytest.approx(2 / 4, abs=0.01)

    def test_zero_score(self) -> None:
        """PR with no issue, empty body, and unrelated tests scores 0."""
        score = score_pr_quality(
            title="Quick fix",
            body="",
            changed_files=["src/auth.py", "other/test_foo.py"],
            test_files=["other/test_foo.py"],
        )
        assert score == pytest.approx(0.0)

    def test_jira_ticket_ref(self) -> None:
        """JIRA-style ticket reference in body counts as issue ref."""
        score = score_pr_quality(
            title="Fix auth redirect",
            body="Fixes PROJ-1234. Tokens expired silently.",
            changed_files=["src/auth.py", "src/tests/test_auth.py"],
            test_files=["src/tests/test_auth.py"],
        )
        # Issue ref (in body) + body + targeted tests = 3/4
        assert score == pytest.approx(3 / 4, abs=0.01)

    def test_short_body_not_meaningful(self) -> None:
        """Body under 20 chars is not considered meaningful."""
        score = score_pr_quality(
            title="Fix bug",
            body="short",
            changed_files=["src/auth.py", "tests/test_auth.py"],
            test_files=["tests/test_auth.py"],
        )
        # Only test overlap — body too short for signal 2, no issue ref in body
        assert score == pytest.approx(1 / 4, abs=0.01)

    def test_linked_issue_adds_signal(self) -> None:
        """Linked issue present adds the 4th quality signal."""
        score_without = score_pr_quality(
            title="Fix auth redirect",
            body="Tokens were not refreshed on 401 responses.",
            changed_files=["src/auth.py", "tests/test_auth.py"],
            test_files=["tests/test_auth.py"],
        )
        score_with = score_pr_quality(
            title="Fix auth redirect",
            body="Tokens were not refreshed on 401 responses.",
            changed_files=["src/auth.py", "tests/test_auth.py"],
            test_files=["tests/test_auth.py"],
            has_linked_issue=True,
        )
        assert score_with - score_without == pytest.approx(1 / 4, abs=0.01)


# ---------------------------------------------------------------------------
# PRMetadata + resolve tests
# ---------------------------------------------------------------------------


class TestPRMetadata:
    def test_frozen(self) -> None:
        meta = PRMetadata(title="Fix bug")
        try:
            meta.title = "changed"  # type: ignore[misc]
            assert False, "Should raise FrozenInstanceError"
        except AttributeError:
            pass

    def test_defaults(self) -> None:
        meta = PRMetadata(title="t")
        assert meta.body == ""
        assert meta.labels == ()
        assert meta.source_tier == "bare"


class TestFormatTaskDescription:
    def test_with_body_and_labels(self) -> None:
        meta = PRMetadata(
            title="Fix login redirect",
            body="Users were redirected to /home instead of /dashboard after login.",
            labels=("bug", "auth"),
            source_tier="api",
        )
        desc = _format_task_description(meta)
        assert "Fix login redirect" in desc
        assert "redirected to /home" in desc
        assert "Labels: bug, auth" in desc

    def test_bare_no_empty_lines(self) -> None:
        meta = PRMetadata(title="Merge commit abc12345", source_tier="bare")
        desc = _format_task_description(meta)
        assert desc == "Merge commit abc12345"
        assert "\n\n" not in desc

    def test_body_truncated(self) -> None:
        long_body = "x" * 3000
        meta = PRMetadata(title="Big PR", body=long_body, source_tier="commit_message")
        desc = _format_task_description(meta)
        # Body should be truncated to 2000 chars
        assert len(desc) < 2200


class TestResolvePRMetadata:
    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_github_api_success(self, mock_run: object) -> None:
        """gh pr view returns valid JSON → source_tier=api."""
        gh_json = json.dumps(
            {
                "title": "Fix auth token refresh",
                "body": "Tokens were not refreshed on 401 responses.",
                "labels": [{"name": "bug"}, {"name": "auth"}],
            }
        )

        def _side_effect(cmd, **kwargs):
            if cmd[0] == "gh":
                return subprocess.CompletedProcess(cmd, 0, gh_json, "")
            # git log fallback should not be called
            return subprocess.CompletedProcess(cmd, 1, "", "")

        mock_run.side_effect = _side_effect
        source = RepoSource(host="github", owner="o", repo="r", remote_url="")

        meta = resolve_pr_metadata(
            "abc123", Path("/fake"), source, "Merge pull request #42 from fix/auth"
        )

        assert meta.source_tier == "api"
        assert meta.title == "Fix auth token refresh"
        assert "401" in meta.body
        assert "bug" in meta.labels
        assert "auth" in meta.labels

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_github_api_fallback_to_commit(self, mock_run: object) -> None:
        """gh fails → falls back to git log commit message body."""
        commit_body = "Fix auth token refresh\n\nTokens expired silently."

        def _side_effect(cmd, **kwargs):
            if cmd[0] == "gh":
                return subprocess.CompletedProcess(cmd, 1, "", "gh: not found")
            # git log --format=%B
            return subprocess.CompletedProcess(cmd, 0, commit_body, "")

        mock_run.side_effect = _side_effect
        source = RepoSource(host="github", owner="o", repo="r", remote_url="")

        meta = resolve_pr_metadata(
            "abc123", Path("/fake"), source, "Merge pull request #42 from fix/auth"
        )

        assert meta.source_tier == "commit_message"
        assert "expired silently" in meta.body

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_local_uses_commit_message(self, mock_run: object) -> None:
        """Local source skips API, goes straight to commit message."""
        commit_body = "Add caching\n\nReduce DB queries by caching user profiles."

        mock_run.return_value = subprocess.CompletedProcess(["git"], 0, commit_body, "")
        source = RepoSource(host="local", owner="", repo="r", remote_url="")

        meta = resolve_pr_metadata("abc123", Path("/fake"), source, "Add caching")

        assert meta.source_tier == "commit_message"
        assert "DB queries" in meta.body

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_all_fail_bare_fallback(self, mock_run: object) -> None:
        """All tiers fail → bare fallback."""
        mock_run.return_value = subprocess.CompletedProcess(["git"], 1, "", "error")
        source = RepoSource(host="local", owner="", repo="r", remote_url="")

        meta = resolve_pr_metadata("abc12345dead", Path("/fake"), source, "")

        assert meta.source_tier == "bare"
        assert "abc12345" in meta.title

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_github_gh_not_installed(self, mock_run: object) -> None:
        """gh not on PATH → FileNotFoundError → falls through gracefully."""
        commit_body = "Fix bug\n\nDetails here."

        def _side_effect(cmd, **kwargs):
            if cmd[0] == "gh":
                raise FileNotFoundError("gh not found")
            return subprocess.CompletedProcess(cmd, 0, commit_body, "")

        mock_run.side_effect = _side_effect
        source = RepoSource(host="github", owner="o", repo="r", remote_url="")

        meta = resolve_pr_metadata(
            "abc123", Path("/fake"), source, "Merge pull request #10"
        )

        assert meta.source_tier == "commit_message"
        assert "Details here" in meta.body

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_no_pr_number_in_title_skips_api(self, mock_run: object) -> None:
        """Merge title without #N pattern → skips gh API call."""
        commit_body = "Squash merge\n\nMultiple fixes."

        call_commands: list[list[str]] = []

        def _side_effect(cmd, **kwargs):
            call_commands.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, commit_body, "")

        mock_run.side_effect = _side_effect
        source = RepoSource(host="github", owner="o", repo="r", remote_url="")

        meta = resolve_pr_metadata(
            "abc123", Path("/fake"), source, "Squash merge of feature branch"
        )

        assert meta.source_tier == "commit_message"
        # Should not have called gh at all
        assert not any(c[0] == "gh" for c in call_commands)


# ---------------------------------------------------------------------------
# extract_task_from_merge enrichment tests
# ---------------------------------------------------------------------------


class TestExtractTaskFromMergeEnriched:
    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_with_source_enriches_description(self, mock_run: object) -> None:
        """When source is passed, description is enriched from commit message."""
        files = ["src/auth.py", "tests/test_auth.py"]
        commit_body = "Fix auth\n\nTokens expired silently on 401."

        call_count = {"n": 0}

        def _side_effect(cmd, **kwargs):
            call_count["n"] += 1
            if "diff" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "\n".join(files), "")
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 0, commit_body, "")
            return subprocess.CompletedProcess(cmd, 1, "", "")

        mock_run.side_effect = _side_effect
        source = RepoSource(host="local", owner="", repo="r", remote_url="")

        result = extract_task_from_merge(
            "abc12345deadbeef",
            Path("/fake/myrepo"),
            source=source,
            merge_title="Fix auth",
        )

        assert result is not None
        task, pr_meta = result
        assert "expired silently" in task.metadata.description
        assert pr_meta.source_tier == "commit_message"

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_without_source_returns_none(self, mock_run: object) -> None:
        """No source → bare metadata → hard-gated out."""
        files = ["src/auth.py", "tests/test_auth.py"]
        mock_run.side_effect = _mock_diff_name_only(files)

        result = extract_task_from_merge("abc12345deadbeef", Path("/fake/myrepo"))
        assert result is None


# ---------------------------------------------------------------------------
# extract_subsystems tests
# ---------------------------------------------------------------------------


class TestExtractSubsystems:
    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_basic_subsystem_extraction(self, mock_run: object) -> None:
        """Two merges touching different dirs yield correct prefix counts."""
        prs = [
            MergedPR(sha="aaa", title="PR1", merge_commit="aaa"),
            MergedPR(sha="bbb", title="PR2", merge_commit="bbb"),
        ]
        files_a = "pkg/scheduler/algo.go\npkg/scheduler/algo_test.go\n"
        files_b = "cmd/server/main.go\npkg/scheduler/queue.go\n"

        call = {"n": 0}

        def _side_effect(cmd, **kwargs):
            call["n"] += 1
            if call["n"] == 1:
                return subprocess.CompletedProcess(cmd, 0, files_a, "")
            return subprocess.CompletedProcess(cmd, 0, files_b, "")

        mock_run.side_effect = _side_effect

        result = extract_subsystems(prs, Path("/fake"))

        assert result["pkg/scheduler/"] == 2  # both merges touch it
        assert result["cmd/server/"] == 1

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_depth_1(self, mock_run: object) -> None:
        """Depth=1 gives coarser prefixes."""
        prs = [MergedPR(sha="aaa", title="PR1", merge_commit="aaa")]
        files = "pkg/scheduler/algo.go\npkg/api/types.go\ncmd/server/main.go\n"
        mock_run.side_effect = _mock_subprocess(stdout=files)

        result = extract_subsystems(prs, Path("/fake"), depth=1)

        assert "pkg/" in result
        assert "cmd/" in result
        assert "pkg/scheduler/" not in result

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_root_level_files_skipped(self, mock_run: object) -> None:
        """Files at repo root (no directory) are excluded."""
        prs = [MergedPR(sha="aaa", title="PR1", merge_commit="aaa")]
        files = "README.md\nMakefile\npkg/auth/auth.go\n"
        mock_run.side_effect = _mock_subprocess(stdout=files)

        result = extract_subsystems(prs, Path("/fake"))

        assert "pkg/auth/" in result
        assert len(result) == 1  # only pkg/auth/, root files skipped

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_empty_merge(self, mock_run: object) -> None:
        """Merge with no changed files contributes nothing."""
        prs = [MergedPR(sha="aaa", title="PR1", merge_commit="aaa")]
        mock_run.side_effect = _mock_subprocess(stdout="")

        result = extract_subsystems(prs, Path("/fake"))
        assert result == {}


# ---------------------------------------------------------------------------
# mine_tasks subsystem filter tests
# ---------------------------------------------------------------------------


class TestMineTasksSubsystemFilter:
    @patch("codeprobe.mining.extractor.subprocess.run")
    @patch("codeprobe.mining.sources.subprocess.run")
    def test_subsystem_filter(
        self, mock_sources_run: object, mock_extractor_run: object
    ) -> None:
        """Only merges touching the requested subsystem are included."""
        merge_log = (
            "aaaa1111bbbb2222 Merge pull request #1 from fix/auth\n"
            "cccc3333dddd4444 Merge pull request #2 from fix/server\n"
        )
        auth_files = "pkg/auth/auth.go\ntests/test_auth.go\n"
        server_files = "cmd/server/main.go\ncmd/server/main_test.go\n"

        mock_sources_run.side_effect = _mock_git_remote("https://github.com/o/r.git\n")

        diff_call = {"n": 0}

        def _extractor_side_effect(cmd, **kwargs):
            if "log" in cmd:
                if "--merges" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, merge_log, "")
                # Commit body for PR metadata
                return subprocess.CompletedProcess(
                    cmd, 0, "Fix something\n\nDetailed description of the fix.", ""
                )
            if "diff" in cmd:
                diff_call["n"] += 1
                if diff_call["n"] == 1:
                    return subprocess.CompletedProcess(cmd, 0, auth_files, "")
                return subprocess.CompletedProcess(cmd, 0, server_files, "")
            return subprocess.CompletedProcess(cmd, 1, "", "")

        mock_extractor_run.side_effect = _extractor_side_effect

        # Filter to only pkg/ subsystem
        tasks = mine_tasks(
            Path("/fake/repo"), count=5, subsystems=("pkg/",), min_quality=0.0
        )

        assert len(tasks) == 1
        assert tasks[0].metadata.language == "go"

    @patch("codeprobe.mining.extractor.subprocess.run")
    @patch("codeprobe.mining.sources.subprocess.run")
    def test_empty_subsystem_no_filter(
        self, mock_sources_run: object, mock_extractor_run: object
    ) -> None:
        """Empty subsystems tuple means no filtering (backward compatible)."""
        merge_log = "aaaa1111bbbb2222 Merge pull request #1 from fix/auth\n"
        diff_files = "pkg/auth/auth.go\ntests/test_auth.go\n"

        mock_sources_run.side_effect = _mock_git_remote("https://github.com/o/r.git\n")

        def _extractor_side_effect(cmd, **kwargs):
            if "log" in cmd:
                if "--merges" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, merge_log, "")
                return subprocess.CompletedProcess(
                    cmd, 0, "Fix auth\n\nDetailed description.", ""
                )
            if "diff" in cmd:
                return subprocess.CompletedProcess(cmd, 0, diff_files, "")
            return subprocess.CompletedProcess(cmd, 1, "", "")

        mock_extractor_run.side_effect = _extractor_side_effect

        tasks = mine_tasks(Path("/fake/repo"), count=5, subsystems=(), min_quality=0.0)
        assert len(tasks) == 1


# ---------------------------------------------------------------------------
# run_mine stale-task clearing tests
# ---------------------------------------------------------------------------


class TestRunMineClearsStale:
    @patch("codeprobe.mining.extractor.subprocess.run")
    @patch("codeprobe.mining.sources.subprocess.run")
    def test_clears_stale_tasks(
        self, mock_sources_run: object, mock_extractor_run: object, tmp_path: Path
    ) -> None:
        """Prior task directories are removed before writing new tasks."""
        from codeprobe.cli.mine_cmd import run_mine

        # Create a stale task directory
        stale_dir = tmp_path / ".codeprobe" / "tasks" / "old-stale-task"
        stale_dir.mkdir(parents=True)
        (stale_dir / "instruction.md").write_text("stale")

        merge_log = "aaaa1111bbbb2222 Merge pull request #1 from fix/auth\n"
        diff_files = "src/auth.py\ntests/test_auth.py\n"

        mock_sources_run.side_effect = _mock_git_remote("https://github.com/o/r.git\n")

        def _extractor_side_effect(cmd, **kwargs):
            if "log" in cmd:
                if "--merges" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, merge_log, "")
                return subprocess.CompletedProcess(
                    cmd, 0, "Fix auth\n\nTokens expired on 401.", ""
                )
            if "diff" in cmd:
                return subprocess.CompletedProcess(cmd, 0, diff_files, "")
            return subprocess.CompletedProcess(cmd, 1, "", "")

        mock_extractor_run.side_effect = _extractor_side_effect

        run_mine(str(tmp_path), count=5)

        tasks_dir = tmp_path / ".codeprobe" / "tasks"
        task_dirs = [d for d in tasks_dir.iterdir() if d.is_dir()]

        # Stale task gone, only new task present
        assert not stale_dir.exists()
        assert len(task_dirs) == 1
        assert task_dirs[0].name == "aaaa1111"


# ---------------------------------------------------------------------------
# _extract_issue_numbers tests
# ---------------------------------------------------------------------------


class TestExtractIssueNumbers:
    def test_fixes_pattern(self) -> None:
        assert _extract_issue_numbers("Fixes #123") == [123]

    def test_closes_pattern(self) -> None:
        assert _extract_issue_numbers("Closes #456") == [456]

    def test_resolves_pattern(self) -> None:
        assert _extract_issue_numbers("Resolves #789") == [789]

    def test_case_insensitive(self) -> None:
        assert _extract_issue_numbers("fixes #10") == [10]
        assert _extract_issue_numbers("FIXES #10") == [10]
        assert _extract_issue_numbers("FiXeS #10") == [10]

    def test_variant_forms(self) -> None:
        assert _extract_issue_numbers("fix #1") == [1]
        assert _extract_issue_numbers("close #2") == [2]
        assert _extract_issue_numbers("closed #3") == [3]
        assert _extract_issue_numbers("resolve #4") == [4]
        assert _extract_issue_numbers("resolved #5") == [5]

    def test_multiple_issues(self) -> None:
        body = "Fixes #10, Closes #20, Resolves #30"
        assert _extract_issue_numbers(body) == [10, 20, 30]

    def test_deduplicated(self) -> None:
        body = "Fixes #10. Also fixes #10."
        assert _extract_issue_numbers(body) == [10]

    def test_no_matches(self) -> None:
        assert _extract_issue_numbers("No issue refs here") == []
        assert _extract_issue_numbers("") == []

    def test_mixed_case_multiple(self) -> None:
        body = "fixes #1\nCloses #2\nRESOLVES #3"
        assert _extract_issue_numbers(body) == [1, 2, 3]


# ---------------------------------------------------------------------------
# Issue-based instruction tests
# ---------------------------------------------------------------------------


class TestWriteTaskDirWithIssue:
    def test_instruction_with_issue(self, tmp_path: Path) -> None:
        """Writer generates issue-based instruction when issue data present."""
        task = Task(
            id="issue01",
            repo="myrepo",
            metadata=TaskMetadata(
                name="merge-issue01",
                difficulty="easy",
                description="Fix auth token refresh",
                language="python",
                issue_title="Auth tokens expire silently",
                issue_body="When the server returns 401, the client should refresh tokens.",
            ),
            verification=TaskVerification(
                type="test_script",
                command="pytest tests/test_auth.py",
                reward_type="binary",
            ),
        )
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)
        instruction = (result_path / "instruction.md").read_text(encoding="utf-8")

        # Should use issue title as heading
        assert "# Auth tokens expire silently" in instruction
        # Should contain issue body in Problem section
        assert "## Problem" in instruction
        assert "refresh tokens" in instruction
        # Should contain repo and language info
        assert "**Repository:** myrepo" in instruction
        assert "**Language:** python" in instruction
        # Should have task contract
        assert "TASK_REPO_ROOT=" in instruction
        # Should NOT contain the PR description (that's the solution)
        assert "Fix auth token refresh" not in instruction.split("# Auth tokens")[1]

    def test_instruction_without_issue(self, tmp_path: Path) -> None:
        """Writer generates fallback instruction without issue data."""
        task = Task(
            id="noissue1",
            repo="myrepo",
            metadata=TaskMetadata(
                name="merge-noissue1",
                difficulty="easy",
                description="Fix auth redirect\n\nTokens were not refreshed on 401.",
                language="python",
            ),
            verification=TaskVerification(
                type="test_script",
                command="pytest tests/test_auth.py",
                reward_type="binary",
            ),
        )
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)
        instruction = (result_path / "instruction.md").read_text(encoding="utf-8")

        # Should use task name as heading (fallback)
        assert "# merge-noissue1" in instruction
        # Should have repo/language
        assert "**Repository:** myrepo" in instruction
        assert "**Language:** python" in instruction
        # Should have Task section
        assert "## Task" in instruction
        # Should contain first paragraph hint
        assert "Tokens were not refreshed" in instruction
        # Should have task contract
        assert "TASK_REPO_ROOT=" in instruction


# ---------------------------------------------------------------------------
# LLM enrichment tests
# ---------------------------------------------------------------------------


class TestEnrichment:
    def _make_task(self, quality_score: float, task_id: str = "t001") -> Task:
        """Create a task with a given quality_score."""
        return Task(
            id=task_id,
            repo="myrepo",
            metadata=TaskMetadata(
                name=f"merge-{task_id}",
                difficulty="easy",
                description="Fix auth token refresh\n\nTokens expired silently.",
                language="python",
                quality_score=quality_score,
            ),
            verification=TaskVerification(
                type="test_script",
                command="pytest tests/test_auth.py",
                reward_type="binary",
            ),
        )

    @patch("codeprobe.core.llm.call_claude")
    def test_enrich_task_success(self, mock_call: object) -> None:
        """enrich_task() enriches description and sets enrichment_source='llm'."""
        from codeprobe.core.llm import LLMResponse

        mock_call.return_value = LLMResponse(
            text='{"problem_statement": "Auth tokens expire without warning.", '
            '"acceptance_criteria": "- Tokens refresh on 401\\n- User stays logged in", '
            '"difficulty": "medium"}'
        )

        task = self._make_task(quality_score=0.25)
        enriched = enrich_task(task)

        assert enriched.metadata.enrichment_source == "llm"
        assert "Auth tokens expire without warning" in enriched.metadata.description
        assert "Acceptance Criteria" in enriched.metadata.description
        assert enriched.metadata.difficulty == "medium"
        # Original description preserved
        assert "Tokens expired silently" in enriched.metadata.description

    @patch("codeprobe.core.llm.call_claude")
    def test_enrich_task_llm_failure_returns_unchanged(self, mock_call: object) -> None:
        """On LLM failure, task is returned unchanged."""
        from codeprobe.core.llm import LLMExecutionError

        mock_call.side_effect = LLMExecutionError("timeout")

        task = self._make_task(quality_score=0.25)
        result = enrich_task(task)

        assert result.metadata.enrichment_source == ""
        assert result.metadata.description == task.metadata.description

    @patch("codeprobe.core.llm.call_claude")
    def test_enrich_task_invalid_json_returns_unchanged(
        self, mock_call: object
    ) -> None:
        """On invalid JSON from LLM, task is returned unchanged."""
        from codeprobe.core.llm import LLMResponse

        mock_call.return_value = LLMResponse(text="not valid json at all")

        task = self._make_task(quality_score=0.25)
        result = enrich_task(task)

        assert result.metadata.enrichment_source == ""

    @patch("codeprobe.core.llm.call_claude")
    def test_enrich_tasks_filters_by_quality(self, mock_call: object) -> None:
        """Only tasks with quality_score < 0.5 are enriched."""
        from codeprobe.core.llm import LLMResponse

        mock_call.return_value = LLMResponse(
            text='{"problem_statement": "Enriched.", "acceptance_criteria": "- Done", "difficulty": "easy"}'
        )

        low_quality = self._make_task(quality_score=0.25, task_id="low1")
        high_quality = self._make_task(quality_score=0.75, task_id="high1")

        result = enrich_tasks([low_quality, high_quality])

        assert len(result) == 2
        # Low-quality task was enriched
        assert result[0].metadata.enrichment_source == "llm"
        assert "Enriched." in result[0].metadata.description
        # High-quality task was NOT enriched
        assert result[1].metadata.enrichment_source == ""
        assert result[1].metadata.description == high_quality.metadata.description
        # call_claude was only called once (for the low-quality task)
        mock_call.assert_called_once()

    def test_quality_score_in_metadata(self) -> None:
        """TaskMetadata includes quality_score field."""
        task = self._make_task(quality_score=0.75)
        assert task.metadata.quality_score == 0.75

    def test_enrichment_source_defaults_empty(self) -> None:
        """TaskMetadata.enrichment_source defaults to empty string."""
        meta = TaskMetadata(name="test")
        assert meta.enrichment_source == ""
        assert meta.quality_score == 0.0

    @patch("codeprobe.core.llm.call_claude")
    def test_enrich_tasks_all_high_quality_no_calls(self, mock_call: object) -> None:
        """When all tasks are high quality, call_claude is never invoked."""
        tasks = [
            self._make_task(quality_score=0.5, task_id="h1"),
            self._make_task(quality_score=0.75, task_id="h2"),
        ]

        result = enrich_tasks(tasks)

        assert len(result) == 2
        assert all(t.metadata.enrichment_source == "" for t in result)
        mock_call.assert_not_called()
