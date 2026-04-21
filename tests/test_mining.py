"""Tests for the task mining module."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.mining.extractor import (
    MergedPR,
    PRMetadata,
    _build_test_command,
    _discover_colocated_test_files,
    _extract_issue_numbers,
    _format_task_description,
    enrich_task,
    enrich_tasks,
    extract_subsystems,
    extract_task_from_merge,
    generate_instruction,
    generate_instructions,
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

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_gh_pr_list_for_github_repos(self, mock_run: object) -> None:
        """GitHub repos use gh pr list which captures squash merges."""
        gh_json = json.dumps(
            [
                {"mergeCommit": {"oid": "aaa111"}, "title": "feat: add search"},
                {"mergeCommit": {"oid": "bbb222"}, "title": "fix: login redirect"},
            ]
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=gh_json, stderr=""
        )
        source = RepoSource(host="github", owner="org", repo="app", remote_url="")

        prs = list_merged_prs(source, Path("/fake"), limit=10)

        assert len(prs) == 2
        assert prs[0].sha == "aaa111"
        assert prs[0].title == "feat: add search"
        assert prs[1].sha == "bbb222"
        # Verify gh was called, not git log
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "gh"

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_gh_fallback_to_git_log(self, mock_run: object) -> None:
        """Falls back to git log --merges when gh fails."""
        log_output = "ccc333 Merge pull request #5 from fix/typo\n"

        def _side_effect(cmd, **kwargs):
            if cmd[0] == "gh":
                return subprocess.CompletedProcess(
                    args=cmd, returncode=1, stdout="", stderr="not logged in"
                )
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=log_output, stderr=""
            )

        mock_run.side_effect = _side_effect
        source = RepoSource(host="github", owner="org", repo="app", remote_url="")

        prs = list_merged_prs(source, Path("/fake"), limit=10)

        assert len(prs) == 1
        assert prs[0].sha == "ccc333"

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_non_github_skips_gh(self, mock_run: object) -> None:
        """Non-GitHub hosts go straight to git log without trying gh."""
        log_output = "ddd444 Merge branch 'feature' into 'main'\n"
        mock_run.side_effect = _mock_git_log(log_output)
        source = RepoSource(host="gitlab", owner="org", repo="app", remote_url="")

        prs = list_merged_prs(source, Path("/fake"), limit=10)

        assert len(prs) == 1
        # Verify git was called, not gh
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "git"


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
        assert task.metadata.ground_truth_commit == "abc12345deadbeef"
        assert pr_meta.source_tier == "commit_message"

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_extract_bare_metadata_synthesized(self, mock_run: object) -> None:
        """Bare metadata with files: description is synthesized (br7.4 relaxation)."""
        files = ["src/auth.py", "tests/test_auth.py", "README.md"]
        mock_run.side_effect = _mock_diff_name_only(files)

        # No source → bare metadata, but files are present → synthesized description
        result = extract_task_from_merge("abc12345deadbeef", Path("/fake/myrepo"))
        assert result is not None
        task, pr_meta = result
        assert pr_meta.source_tier == "bare"
        # Synthesized body references changed files
        assert any(f in task.metadata.description for f in files)

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

        result = mine_tasks(Path("/fake/repo"), count=5)

        assert len(result.tasks) == 2
        assert all(isinstance(t, Task) for t in result.tasks)

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

        result = mine_tasks(Path("/fake/repo"), count=5)
        assert result.tasks == []

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
        result = mine_tasks(Path("/fake/repo"), count=5)
        assert len(result.tasks) == 1
        assert result.tasks[0].id == "aaaa1111"

    @patch("codeprobe.mining.extractor.subprocess.run")
    @patch("codeprobe.mining.sources.subprocess.run")
    def test_mine_tasks_bare_metadata_survives_with_low_min_quality(
        self, mock_sources_run: object, mock_extractor_run: object
    ) -> None:
        """Bare metadata with files synthesizes a description (br7.4 relaxation);
        survives when min_quality is low enough to admit synthesized-only tasks."""
        merge_log = "aaaa1111bbbb2222 Squash merge\n"
        # Non-overlapping stems so Signal 3 (test/source name overlap) does NOT
        # fire — the synthesized body alone yields 1/4, below default 0.5.
        diff_files = "src/misc.py\ntests/test_other.py\n"

        mock_sources_run.side_effect = _mock_git_remote("https://github.com/o/r.git\n")

        def _extractor_side_effect(cmd, **kwargs):
            if cmd[0] == "gh":
                return subprocess.CompletedProcess(cmd, 1, "", "")
            if "log" in cmd:
                if "--merges" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, merge_log, "")
                # No commit body → bare metadata
                return subprocess.CompletedProcess(cmd, 1, "", "")
            if "diff" in cmd:
                return subprocess.CompletedProcess(cmd, 0, diff_files, "")
            return subprocess.CompletedProcess(cmd, 1, "", "")

        mock_extractor_run.side_effect = _extractor_side_effect

        # Default min_quality (0.5) filters the synthesized-only task
        default_result = mine_tasks(Path("/fake/repo"), count=5)
        assert default_result.tasks == []

        # With min_quality=0.0 the synthesized bare task is kept
        relaxed_result = mine_tasks(Path("/fake/repo"), count=5, min_quality=0.0)
        assert len(relaxed_result.tasks) == 1
        assert relaxed_result.tasks[0].id == "aaaa1111"

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

        result = mine_tasks(Path("/fake/repo"), count=3)
        assert len(result.tasks) == 3


# ---------------------------------------------------------------------------
# write_task_dir tests
# ---------------------------------------------------------------------------


class TestWriteTaskDir:
    def test_write_task_dir_rejects_path_traversal(self, tmp_path: Path) -> None:
        """Writer rejects task IDs with path traversal."""
        task = Task(
            id="../etc",
            repo="r",
            metadata=TaskMetadata(name="x"),
            verification=TaskVerification(command="bash tests/test.sh"),
        )
        with pytest.raises(ValueError, match="Invalid task id"):
            write_task_dir(task, tmp_path / "tasks", tmp_path / "r")

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
                description=(
                    "Fix auth token refresh\n\nTokens were not refreshed on 401."
                    "\nThis caused silent logout.\n\nLabels: bug, auth"
                ),
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

    def test_go_filters_missing_packages(self, tmp_path: Path) -> None:
        """Go packages that don't exist in repo_path are dropped."""
        (tmp_path / "pkg" / "real").mkdir(parents=True)
        result = _build_test_command(
            "go",
            ["pkg/real/foo_test.go", "pkg/missing/bar_test.go"],
            repo_path=tmp_path,
        )
        assert "./pkg/real/..." in result
        assert "missing" not in result

    def test_go_all_missing_falls_back(self, tmp_path: Path) -> None:
        """When all Go packages are missing, falls back to default."""
        result = _build_test_command(
            "go",
            ["vendor/gone/x_test.go"],
            repo_path=tmp_path,
        )
        assert result == "bash tests/test.sh"

    def test_python_filters_missing_files(self, tmp_path: Path) -> None:
        """Python test files that don't exist in repo_path are dropped."""
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_real.py").touch()
        result = _build_test_command(
            "python",
            ["tests/test_real.py", "tests/test_gone.py"],
            repo_path=tmp_path,
        )
        assert "test_real.py" in result
        assert "test_gone.py" not in result

    def test_no_repo_path_skips_validation(self) -> None:
        """Without repo_path, all paths are kept (backward compat)."""
        result = _build_test_command(
            "go",
            ["pkg/might/not/exist/x_test.go"],
        )
        assert "./pkg/might/not/exist/..." in result


# ---------------------------------------------------------------------------
# _build_test_command removal verification tests
# ---------------------------------------------------------------------------


class TestBuildTestCommandRemoval:
    def test_go_removal_when_all_packages_deleted(self) -> None:
        """When all test packages are in deleted dirs, generate removal check."""
        result = _build_test_command(
            "go",
            ["cluster/images/etcd/migrate/migrate_test.go"],
            deleted_dirs={"cluster/images/etcd/migrate"},
        )
        assert "test ! -d" in result
        assert "go test" not in result
        assert "cluster/images/etcd/migrate" in result

    def test_go_removal_nested_deleted_dir(self) -> None:
        """Packages under a deleted parent are detected as removal."""
        result = _build_test_command(
            "go",
            [
                "legacy/pkg/a/a_test.go",
                "legacy/pkg/b/b_test.go",
            ],
            deleted_dirs={"legacy"},
        )
        assert "test ! -d" in result
        assert "legacy/pkg/a" in result
        assert "legacy/pkg/b" in result

    def test_go_mixed_deleted_and_existing(self, tmp_path: Path) -> None:
        """When some packages are deleted but others exist, use go test for survivors."""
        (tmp_path / "pkg" / "alive").mkdir(parents=True)
        result = _build_test_command(
            "go",
            ["pkg/alive/x_test.go", "pkg/dead/y_test.go"],
            repo_path=tmp_path,
            deleted_dirs={"pkg/dead"},
        )
        # Not all packages are deleted, so no removal check
        assert "go test" in result
        assert "pkg/alive" in result
        # Dead package filtered by path validation
        assert "pkg/dead" not in result

    def test_go_no_deleted_dirs_normal_flow(self) -> None:
        """Without deleted_dirs, normal go test command."""
        result = _build_test_command(
            "go",
            ["pkg/auth/auth_test.go"],
            deleted_dirs=None,
        )
        assert result == "go test ./pkg/auth/..."

    def test_go_empty_deleted_dirs_normal_flow(self) -> None:
        """Empty deleted_dirs set treated same as None."""
        result = _build_test_command(
            "go",
            ["pkg/auth/auth_test.go"],
            deleted_dirs=set(),
        )
        assert result == "go test ./pkg/auth/..."


# ---------------------------------------------------------------------------
# _discover_colocated_test_files tests
# ---------------------------------------------------------------------------


class TestDiscoverColocatedTestFiles:
    def test_finds_go_test_files(self, tmp_path: Path) -> None:
        """Discovers *_test.go files in the same package as changed .go files."""
        pkg = tmp_path / "internal" / "auth"
        pkg.mkdir(parents=True)
        (pkg / "auth.go").touch()
        (pkg / "auth_test.go").touch()
        (pkg / "middleware.go").touch()

        result = _discover_colocated_test_files(["internal/auth/auth.go"], tmp_path)
        assert "internal/auth/auth_test.go" in result

    def test_finds_python_test_files(self, tmp_path: Path) -> None:
        pkg = tmp_path / "src" / "utils"
        pkg.mkdir(parents=True)
        (pkg / "helpers.py").touch()
        (pkg / "test_helpers.py").touch()

        result = _discover_colocated_test_files(["src/utils/helpers.py"], tmp_path)
        assert "src/utils/test_helpers.py" in result

    def test_no_test_files_returns_empty(self, tmp_path: Path) -> None:
        pkg = tmp_path / "src"
        pkg.mkdir()
        (pkg / "main.go").touch()

        result = _discover_colocated_test_files(["src/main.go"], tmp_path)
        assert result == []

    def test_nonexistent_repo_returns_empty(self) -> None:
        result = _discover_colocated_test_files(["pkg/foo.go"], Path("/nonexistent"))
        assert result == []

    def test_root_level_files_skipped(self, tmp_path: Path) -> None:
        """Files at repo root (parent='.') are skipped."""
        (tmp_path / "main.go").touch()
        (tmp_path / "main_test.go").touch()

        result = _discover_colocated_test_files(["main.go"], tmp_path)
        assert result == []


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
            "abc1234", Path("/fake"), source, "Merge pull request #42 from fix/auth"
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
            "abc1234", Path("/fake"), source, "Merge pull request #42 from fix/auth"
        )

        assert meta.source_tier == "commit_message"
        assert "expired silently" in meta.body

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_local_uses_commit_message(self, mock_run: object) -> None:
        """Local source skips API, goes straight to commit message."""
        commit_body = "Add caching\n\nReduce DB queries by caching user profiles."

        mock_run.return_value = subprocess.CompletedProcess(["git"], 0, commit_body, "")
        source = RepoSource(host="local", owner="", repo="r", remote_url="")

        meta = resolve_pr_metadata("abc1234", Path("/fake"), source, "Add caching")

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
            "abc1234", Path("/fake"), source, "Merge pull request #10"
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
            "abc1234", Path("/fake"), source, "Squash merge of feature branch"
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
    def test_without_source_uses_synthesized_body(self, mock_run: object) -> None:
        """No source → bare metadata with files: description is synthesized (br7.4)."""
        files = ["src/auth.py", "tests/test_auth.py"]
        mock_run.side_effect = _mock_diff_name_only(files)

        result = extract_task_from_merge("abc12345deadbeef", Path("/fake/myrepo"))
        assert result is not None
        task, pr_meta = result
        assert pr_meta.source_tier == "bare"
        assert "src/auth.py" in task.metadata.description


# ---------------------------------------------------------------------------
# extract_subsystems tests
# ---------------------------------------------------------------------------


class TestExtractSubsystems:
    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_basic_subsystem_extraction(self, mock_run: object) -> None:
        """Two merges touching different dirs yield correct prefix counts."""
        prs = [
            MergedPR(sha="aaa1111", title="PR1", merge_commit="aaa1111"),
            MergedPR(sha="bbb2222", title="PR2", merge_commit="bbb2222"),
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
        prs = [MergedPR(sha="aaa1111", title="PR1", merge_commit="aaa1111")]
        files = "pkg/scheduler/algo.go\npkg/api/types.go\ncmd/server/main.go\n"
        mock_run.side_effect = _mock_subprocess(stdout=files)

        result = extract_subsystems(prs, Path("/fake"), depth=1)

        assert "pkg/" in result
        assert "cmd/" in result
        assert "pkg/scheduler/" not in result

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_root_level_files_skipped(self, mock_run: object) -> None:
        """Files at repo root (no directory) are excluded."""
        prs = [MergedPR(sha="aaa1111", title="PR1", merge_commit="aaa1111")]
        files = "README.md\nMakefile\npkg/auth/auth.go\n"
        mock_run.side_effect = _mock_subprocess(stdout=files)

        result = extract_subsystems(prs, Path("/fake"))

        assert "pkg/auth/" in result
        assert len(result) == 1  # only pkg/auth/, root files skipped

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_empty_merge(self, mock_run: object) -> None:
        """Merge with no changed files contributes nothing."""
        prs = [MergedPR(sha="aaa1111", title="PR1", merge_commit="aaa1111")]
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
        result = mine_tasks(
            Path("/fake/repo"), count=5, subsystems=("pkg/",), min_quality=0.0
        )

        assert len(result.tasks) == 1
        assert result.tasks[0].metadata.language == "go"

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

        result = mine_tasks(Path("/fake/repo"), count=5, subsystems=(), min_quality=0.0)
        assert len(result.tasks) == 1


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

        # Make tmp_path look like a git repo so the structural check passes.
        (tmp_path / ".git").mkdir()

        # Create a stale task directory
        stale_dir = tmp_path / ".codeprobe" / "tasks" / "old-stale-task"
        stale_dir.mkdir(parents=True)
        (stale_dir / "instruction.md").write_text("stale")

        # Create test file paths so path validation passes
        (tmp_path / "tests").mkdir(exist_ok=True)
        (tmp_path / "tests" / "test_auth.py").touch()

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

        run_mine(str(tmp_path), count=5, no_llm=True)

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
        """enrich_task() generates instruction via LLM and sets enrichment_source='llm'."""
        from codeprobe.core.llm import LLMResponse

        mock_call.return_value = LLMResponse(
            text='{"heading": "Fix silent token expiry", '
            '"problem": "Auth tokens expire without warning, causing silent 401 errors.", '
            '"requirements": "- Tokens refresh on 401\\n- User stays logged in", '
            '"difficulty": "medium"}'
        )

        task = self._make_task(quality_score=0.25)
        enriched = enrich_task(task)

        assert enriched.metadata.enrichment_source == "llm"
        assert enriched.metadata.issue_title == "Fix silent token expiry"
        assert "Auth tokens expire without warning" in enriched.metadata.issue_body
        assert "Requirements" in enriched.metadata.issue_body
        assert enriched.metadata.difficulty == "medium"
        # Original description preserved in metadata.description
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
            text='{"heading": "Fix auth", "problem": "Enriched problem.", '
            '"requirements": "- Done", "difficulty": "easy"}'
        )

        low_quality = self._make_task(quality_score=0.25, task_id="low1")
        high_quality = self._make_task(quality_score=0.75, task_id="high1")

        result = enrich_tasks([low_quality, high_quality])

        assert len(result) == 2
        # Low-quality task was enriched
        assert result[0].metadata.enrichment_source == "llm"
        assert "Enriched problem." in result[0].metadata.issue_body
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


# ---------------------------------------------------------------------------
# LLM instruction generation tests
# ---------------------------------------------------------------------------


class TestGenerateInstruction:
    def _make_task(self, task_id: str = "t001") -> Task:
        return Task(
            id=task_id,
            repo="kubernetes",
            metadata=TaskMetadata(
                name=f"merge-{task_id}",
                difficulty="hard",
                description="Add workload aware preemption\n\n"
                "#### What type of PR is this?\n/kind feature\n\n"
                "#### What this PR does:\nImplements KEP-5710.",
                language="go",
            ),
            verification=TaskVerification(command="go test ./pkg/scheduler/..."),
        )

    @patch("codeprobe.core.llm.call_claude")
    def test_generate_instruction_success(self, mock_call: object) -> None:
        """generate_instruction() produces clean instruction from raw PR data."""
        from codeprobe.core.llm import LLMResponse

        mock_call.return_value = LLMResponse(
            text='{"heading": "Implement workload-aware pod preemption", '
            '"problem": "When scheduling pod groups, the default preemption runs per-pod '
            'instead of per-group, leading to suboptimal scheduling decisions.", '
            '"requirements": "- Preemption runs for the whole pod group\\n'
            '- Existing per-pod preemption still works for non-group pods", '
            '"difficulty": "hard"}'
        )

        task = self._make_task()
        result = generate_instruction(
            task,
            pr_body="Raw PR body with template noise",
            changed_files=[
                "pkg/scheduler/preemption.go",
                "pkg/scheduler/preemption_test.go",
            ],
        )

        assert result.metadata.enrichment_source == "llm"
        assert result.metadata.issue_title == "Implement workload-aware pod preemption"
        assert "suboptimal scheduling" in result.metadata.issue_body
        assert "Requirements" in result.metadata.issue_body
        assert result.metadata.difficulty == "hard"
        # Original raw description preserved
        assert "KEP-5710" in result.metadata.description

    @patch("codeprobe.core.llm.call_claude")
    def test_generate_instruction_llm_failure_returns_unchanged(
        self, mock_call: object
    ) -> None:
        from codeprobe.core.llm import LLMExecutionError

        mock_call.side_effect = LLMExecutionError("timeout")
        task = self._make_task()
        result = generate_instruction(task)

        assert result.metadata.enrichment_source == ""
        assert result.metadata.issue_title == ""

    @patch("codeprobe.core.llm.call_claude")
    def test_generate_instruction_empty_problem_returns_unchanged(
        self, mock_call: object
    ) -> None:
        from codeprobe.core.llm import LLMResponse

        mock_call.return_value = LLMResponse(
            text='{"heading": "Something", "problem": "", "requirements": "", "difficulty": "easy"}'
        )
        task = self._make_task()
        result = generate_instruction(task)

        # Empty problem → keep original task unchanged
        assert result.metadata.enrichment_source == ""

    @patch("codeprobe.core.llm.call_claude")
    def test_generate_instructions_batch(self, mock_call: object) -> None:
        """generate_instructions() processes all tasks."""
        from codeprobe.core.llm import LLMResponse

        mock_call.return_value = LLMResponse(
            text='{"heading": "Fix it", "problem": "Something is broken.", '
            '"requirements": "- Fix the thing", "difficulty": "easy"}'
        )

        tasks = [self._make_task(f"t{i}") for i in range(3)]
        results = generate_instructions(tasks)

        assert len(results) == 3
        assert all(r.metadata.enrichment_source == "llm" for r in results)
        assert mock_call.call_count == 3

    @patch("codeprobe.core.llm.call_claude")
    def test_llm_instruction_written_without_regex_cleanup(
        self, mock_call: object, tmp_path: Path
    ) -> None:
        """LLM-generated instructions skip regex cleanup in writer."""
        from codeprobe.core.llm import LLMResponse

        mock_call.return_value = LLMResponse(
            text='{"heading": "Fix scheduler preemption", '
            '"problem": "Pod groups fail to schedule with shared claims.", '
            '"requirements": "- Shared claims handled correctly", '
            '"difficulty": "hard"}'
        )

        task = self._make_task()
        enriched = generate_instruction(task)

        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "kubernetes"
        result_path = write_task_dir(enriched, base_dir, repo_path)
        instruction = (result_path / "instruction.md").read_text(encoding="utf-8")

        assert "# Fix scheduler preemption" in instruction
        assert "Pod groups fail to schedule" in instruction
        assert "Shared claims handled correctly" in instruction
        # Template noise from raw description should NOT leak through
        assert "What type of PR is this" not in instruction

    @patch("codeprobe.core.llm.call_claude")
    def test_generate_instruction_csb_style_fields(
        self, mock_call: object, tmp_path: Path
    ) -> None:
        """CSB-style fields (team_context, access_scope, reproduction, \
success_criteria) flow into the written instruction."""
        from codeprobe.core.llm import LLMResponse

        mock_call.return_value = LLMResponse(
            text=json.dumps(
                {
                    "heading": "Implement workload-aware pod preemption",
                    "team_context": (
                        "You are a developer on the Scheduler team. Your "
                        "team owns pkg/scheduler/."
                    ),
                    "access_scope": (
                        "You may modify files under pkg/scheduler/ only; "
                        "other packages are read-only."
                    ),
                    "problem": (
                        "Pod groups currently preempt per-pod rather than as a "
                        "group, causing suboptimal scheduling outcomes."
                    ),
                    "reproduction": (
                        "```bash\nkubectl apply -f podgroup.yaml\n```"
                    ),
                    "requirements": (
                        "- Preemption runs for the whole pod group\n"
                        "- Existing per-pod preemption still works"
                    ),
                    "success_criteria": (
                        "- Pod groups schedule atomically\n"
                        "- All changes are within pkg/scheduler/ only"
                    ),
                    "difficulty": "hard",
                }
            )
        )

        task = self._make_task()
        enriched = generate_instruction(
            task,
            pr_body="",
            changed_files=["pkg/scheduler/preemption.go"],
        )

        body = enriched.metadata.issue_body
        assert "## Context" in body
        assert "Scheduler team" in body
        assert "## Access Scope" in body
        assert "## Steps to Reproduce" in body
        assert "kubectl apply" in body
        assert "## Requirements" in body
        assert "## Success Criteria" in body
        assert "Pod groups schedule atomically" in body

        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "kubernetes"
        result_path = write_task_dir(enriched, base_dir, repo_path)
        instruction = (result_path / "instruction.md").read_text(encoding="utf-8")
        assert "## Context" in instruction
        assert "## Access Scope" in instruction
        assert "## Steps to Reproduce" in instruction
        assert "## Success Criteria" in instruction

    @patch("codeprobe.core.llm.call_claude")
    def test_generate_instruction_uses_sonnet(self, mock_call: object) -> None:
        """Instruction generation requests the sonnet model, not haiku."""
        from codeprobe.core.llm import LLMRequest, LLMResponse

        mock_call.return_value = LLMResponse(
            text='{"heading": "x", "problem": "p.", "requirements": "", '
            '"difficulty": "easy"}'
        )
        generate_instruction(self._make_task())

        assert mock_call.call_count == 1
        args, _ = mock_call.call_args
        request = args[0]
        assert isinstance(request, LLMRequest)
        assert request.model == "sonnet"


# ---------------------------------------------------------------------------
# PR template stripping tests
# ---------------------------------------------------------------------------


class TestStripPrTemplate:
    """Test _strip_pr_template and _extract_first_paragraph with PR templates."""

    def test_kubernetes_style_template(self) -> None:
        """Kubernetes PR bodies have template sections that should be stripped."""
        from codeprobe.mining.writer import _extract_first_paragraph

        description = (
            "Add workload aware preemption\n\n"
            "#### What type of PR is this?\r\n\r\n"
            "/kind feature\r\n\r\n"
            "#### What this PR does / why we need it:\r\n\r\n"
            "This PR implements workload aware preemption from KEP-5710.\r\n\r\n"
            "#### Which issue(s) this PR is related to:\r\n\r\n"
            "KEP-5710\r\n\r\n"
            "#### Special notes for your reviewer:\r\n\r\n"
            "This PR builds on top of two PRs.\r\n\r\n"
            "#### Does this PR introduce a user-facing change?\r\n\r\n"
            "```release-note\nSome release note.\n```\r\n\r\n"
            "#### Additional documentation e.g., KEPs:\r\n\r\n"
            "docs link"
        )
        result = _extract_first_paragraph(description)
        # Should extract the "What this PR does" content, not template headers
        assert "workload aware preemption" in result
        assert "KEP-5710" in result
        # Should NOT contain template section headers
        assert "What type of PR is this" not in result
        assert "/kind feature" not in result

    def test_strip_pr_template_preserves_plain_text(self) -> None:
        """Non-template text should pass through unchanged."""
        from codeprobe.mining.writer import _strip_pr_template

        text = "This fixes a bug where tokens expired silently."
        result = _strip_pr_template(text)
        assert result == text

    def test_strip_pr_template_removes_label_lines(self) -> None:
        """Lines like /kind feature should be removed."""
        from codeprobe.mining.writer import _strip_pr_template

        text = "Some description.\n\n/kind feature\n/area scheduling\n\nMore text."
        result = _strip_pr_template(text)
        assert "/kind feature" not in result
        assert "/area scheduling" not in result
        assert "Some description." in result
        assert "More text." in result

    def test_strip_html_comments(self) -> None:
        """HTML comments from PR templates should be stripped."""
        from codeprobe.mining.writer import _strip_pr_template

        text = (
            "<!--  Thanks for sending a pull request!  Here are some tips:\n"
            "1. Follow the guide\n"
            "2. Do stuff\n"
            "-->\n\n"
            "The actual description of the change."
        )
        result = _strip_pr_template(text)
        assert "Thanks for sending" not in result
        assert "actual description" in result

    def test_strip_html_comments_multiline(self) -> None:
        """Multiline HTML comments should be fully removed."""
        from codeprobe.mining.writer import _strip_pr_template

        text = "Before <!-- comment\nspanning\nmultiple lines --> After"
        result = _strip_pr_template(text)
        assert "comment" not in result
        assert "Before" in result
        assert "After" in result

    def test_strip_release_note_fenced_block(self) -> None:
        """```release-note blocks should be stripped."""
        from codeprobe.mining.writer import _strip_pr_template

        text = (
            "Description here.\n\n"
            "```release-note\n"
            "Some release note content.\n"
            "```\n\n"
            "More text."
        )
        result = _strip_pr_template(text)
        assert "release note content" not in result
        assert "Description here." in result

    def test_instruction_strips_html_comment_pr_template(self, tmp_path: Path) -> None:
        """PR bodies starting with HTML comment template should produce clean instructions."""
        from codeprobe.mining.writer import _extract_first_paragraph

        description = (
            "Fix the widget\n\n"
            "<!--  Thanks for sending a pull request!  Here are some tips for you:\n"
            "1. Read the guide\n"
            "-->\n\n"
            "This fixes the widget rendering bug."
        )
        result = _extract_first_paragraph(description)
        assert "Thanks for sending" not in result
        assert "widget rendering bug" in result

    def test_issue_body_details_blocks_stripped(self, tmp_path: Path) -> None:
        """<details> blocks in issue bodies should be stripped during write."""
        task = Task(
            id="det001",
            repo="myrepo",
            metadata=TaskMetadata(
                name="merge-det001",
                difficulty="easy",
                description="Fix DRA scheduling",
                language="go",
                issue_title="DRA scheduler bug",
                issue_body=(
                    "### What happened?\n\n"
                    "Scheduler loops forever.\n\n"
                    "### Kubernetes version\n\n"
                    "<details>\n\n"
                    "```console\n$ kubectl version\nv1.36.0\n```\n\n"
                    "</details>\n\n"
                    "### OS version\n\n"
                    "<details>\n\n"
                    "```console\n$ uname -a\nLinux 6.17\n```\n\n"
                    "</details>"
                ),
            ),
            verification=TaskVerification(
                type="test_script",
                command="go test ./pkg/...",
                reward_type="binary",
            ),
        )
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"

        result_path = write_task_dir(task, base_dir, repo_path)
        instruction = (result_path / "instruction.md").read_text(encoding="utf-8")

        assert "Scheduler loops forever" in instruction
        assert "<details>" not in instruction
        assert "uname -a" not in instruction

    def test_extract_first_paragraph_skips_empty_paragraphs(self) -> None:
        """After stripping template noise, first non-empty paragraph is used."""
        from codeprobe.mining.writer import _extract_first_paragraph

        description = (
            "Fix bug\n\n"
            "#### What type of PR is this?\n\n"
            "/kind bug\n\n"
            "#### What this PR does / why we need it:\n\n"
            "Tokens expired silently causing 401 errors.\n\n"
            "#### Which issue(s) this PR is related to:\n\n"
            "#1234"
        )
        result = _extract_first_paragraph(description)
        assert "Tokens expired silently" in result

    def test_extract_first_paragraph_fallback_when_no_what_section(self) -> None:
        """PR without 'What this PR does' still extracts first paragraph."""
        from codeprobe.mining.writer import _extract_first_paragraph

        description = "Fix auth redirect\n\nTokens were not refreshed on 401."
        result = _extract_first_paragraph(description)
        assert "Tokens were not refreshed" in result

    def test_instruction_with_kubernetes_pr(self, tmp_path: Path) -> None:
        """Full integration: kubernetes-style PR generates useful instruction."""
        k8s_description = (
            "Add workload aware preemption\n\n"
            "#### What type of PR is this?\n\n"
            "/kind feature\n\n"
            "#### What this PR does / why we need it:\n\n"
            "This PR implements workload aware preemption from KEP-5710.\n\n"
            "#### Which issue(s) this PR is related to:\n\n"
            "KEP-5710\n\n"
            "#### Does this PR introduce a user-facing change?\n\n"
            "```release-note\nSome note.\n```"
        )
        task = Task(
            id="k8stest1",
            repo="kubernetes",
            metadata=TaskMetadata(
                name="merge-k8stest1",
                difficulty="hard",
                description=k8s_description,
                language="go",
            ),
            verification=TaskVerification(
                type="test_script",
                command="go test ./pkg/scheduler/...",
                reward_type="binary",
            ),
        )
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "kubernetes"

        result_path = write_task_dir(task, base_dir, repo_path)
        instruction = (result_path / "instruction.md").read_text(encoding="utf-8")

        # Should contain the actual problem description
        assert "workload aware preemption" in instruction
        assert "KEP-5710" in instruction
        # Should NOT contain template noise
        assert "What type of PR is this" not in instruction
        assert "/kind feature" not in instruction


# ---------------------------------------------------------------------------
# Interactive mine command tests
# ---------------------------------------------------------------------------


class TestMineInteractive:
    """Test the interactive mine workflow functions."""

    def test_quality_review_flags_thin_instructions(self) -> None:
        """Quality review flags tasks with thin descriptions."""
        from codeprobe.cli.mine_cmd import _quality_review

        tasks = [
            Task(
                id="t1",
                repo="r",
                metadata=TaskMetadata(name="merge-t1", description="short"),
                verification=TaskVerification(command="pytest tests/test_a.py"),
            ),
        ]
        warnings = _quality_review(tasks, "General benchmarking", "balanced")
        assert any("thin instructions" in w for w in warnings)

    def test_quality_review_flags_difficulty_mismatch(self) -> None:
        """Quality review flags easy tasks when goal needs hard."""
        from codeprobe.cli.mine_cmd import _quality_review

        tasks = [
            Task(
                id=f"t{i}",
                repo="r",
                metadata=TaskMetadata(
                    name=f"merge-t{i}",
                    difficulty="easy",
                    description="x" * 100,
                ),
                verification=TaskVerification(command="pytest tests/test_a.py"),
            )
            for i in range(4)
        ]
        warnings = _quality_review(tasks, "MCP / tool comparison", "hard")
        assert any("Difficulty mismatch" in w for w in warnings)

    def test_quality_review_flags_no_variance(self) -> None:
        """Quality review flags all-same-difficulty for mixed goals."""
        from codeprobe.cli.mine_cmd import _quality_review

        tasks = [
            Task(
                id=f"t{i}",
                repo="r",
                metadata=TaskMetadata(
                    name=f"merge-t{i}",
                    difficulty="medium",
                    description="x" * 100,
                ),
                verification=TaskVerification(command="pytest tests/test_a.py"),
            )
            for i in range(3)
        ]
        warnings = _quality_review(tasks, "Model comparison", "mixed")
        assert any("No difficulty variance" in w for w in warnings)

    def test_quality_review_no_warnings_for_good_tasks(self) -> None:
        """Quality review returns no warnings for well-formed tasks."""
        from codeprobe.cli.mine_cmd import _quality_review

        tasks = [
            Task(
                id="t1",
                repo="r",
                metadata=TaskMetadata(
                    name="merge-t1",
                    difficulty="easy",
                    description="x" * 100,
                ),
                verification=TaskVerification(command="pytest tests/test_a.py"),
            ),
            Task(
                id="t2",
                repo="r",
                metadata=TaskMetadata(
                    name="merge-t2",
                    difficulty="hard",
                    description="y" * 100,
                ),
                verification=TaskVerification(command="pytest tests/test_b.py"),
            ),
        ]
        warnings = _quality_review(tasks, "General benchmarking", "balanced")
        assert len(warnings) == 0

    def test_quality_review_flags_stub_tests(self) -> None:
        """Quality review flags tasks with generic test stubs."""
        from codeprobe.cli.mine_cmd import _quality_review

        tasks = [
            Task(
                id="t1",
                repo="r",
                metadata=TaskMetadata(
                    name="merge-t1",
                    description="x" * 100,
                ),
                verification=TaskVerification(command="bash tests/test.sh"),
            ),
        ]
        warnings = _quality_review(tasks, "General benchmarking", "balanced")
        assert any("generic test stubs" in w for w in warnings)


# ---------------------------------------------------------------------------
# MCP-specific task mining tests
# ---------------------------------------------------------------------------


class TestMCPGoalConfig:
    """Verify that --goal mcp wires the correct defaults."""

    def test_mcp_goal_sets_min_files_6(self) -> None:
        """MCP goal extras include min_files=6 for cross-file bias."""
        from codeprobe.cli.mine_cmd import _EVAL_GOALS

        mcp_goal = _EVAL_GOALS["mcp"]
        assert mcp_goal["extras"]["min_files"] == 6

    def test_mcp_goal_sets_task_type(self) -> None:
        """MCP goal maps to mcp_tool_usage task_type."""
        from codeprobe.cli.mine_cmd import _EVAL_GOALS

        assert _EVAL_GOALS["mcp"]["task_type"] == "mcp_tool_usage"


class TestMCPInstructionVariant:
    """Tests for instruction_mcp.md generation in write_task_dir."""

    def test_mcp_task_generates_instruction_mcp_md(self, tmp_path: Path) -> None:
        """MCP tasks produce both instruction.md and instruction_mcp.md."""
        task = Task(
            id="mcp00001",
            repo="myrepo",
            metadata=TaskMetadata(
                name="merge-mcp00001",
                difficulty="hard",
                description="Cross-file refactor of auth module",
                language="python",
                task_type="mcp_tool_usage",
                mcp_suite="sourcegraph",
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

        # instruction.md must exist
        assert (result_path / "instruction.md").is_file()

        # instruction_mcp.md must also exist for MCP tasks
        mcp_instruction = result_path / "instruction_mcp.md"
        assert mcp_instruction.is_file()

        content = mcp_instruction.read_text(encoding="utf-8")
        # Must reference MCP tools
        assert "MCP" in content or "mcp" in content.lower()
        assert "keyword_search" in content
        assert "read_file" in content

    def test_non_mcp_task_no_instruction_mcp_md(self, tmp_path: Path) -> None:
        """Non-MCP tasks do NOT produce instruction_mcp.md."""
        task = Task(
            id="sdlc0001",
            repo="myrepo",
            metadata=TaskMetadata(
                name="merge-sdlc0001",
                difficulty="easy",
                description="Fix a small bug",
                language="python",
                task_type="sdlc_code_change",
            ),
            verification=TaskVerification(
                type="test_script",
                command="pytest tests/test_bug.py",
                reward_type="binary",
            ),
        )
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"
        result_path = write_task_dir(task, base_dir, repo_path)

        assert (result_path / "instruction.md").is_file()
        assert not (result_path / "instruction_mcp.md").exists()


class TestMCPSuiteInMetadata:
    """Tests for mcp_suite field in metadata.json."""

    def test_mcp_task_metadata_has_mcp_suite(self, tmp_path: Path) -> None:
        """MCP tasks include mcp_suite in metadata.json."""
        task = Task(
            id="mcp00002",
            repo="myrepo",
            metadata=TaskMetadata(
                name="merge-mcp00002",
                difficulty="hard",
                description="Cross-file refactor",
                language="python",
                task_type="mcp_tool_usage",
                mcp_suite="sourcegraph",
            ),
            verification=TaskVerification(
                type="test_script",
                command="pytest tests/test_refactor.py",
                reward_type="binary",
            ),
        )
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"
        result_path = write_task_dir(task, base_dir, repo_path)

        meta = json.loads((result_path / "metadata.json").read_text(encoding="utf-8"))
        assert meta["metadata"]["mcp_suite"] == "sourcegraph"

    def test_non_mcp_task_metadata_mcp_suite_null(self, tmp_path: Path) -> None:
        """Non-MCP tasks have mcp_suite=null in metadata.json."""
        task = Task(
            id="sdlc0002",
            repo="myrepo",
            metadata=TaskMetadata(
                name="merge-sdlc0002",
                difficulty="easy",
                description="Simple fix",
                language="python",
            ),
            verification=TaskVerification(
                type="test_script",
                command="pytest tests/test_simple.py",
                reward_type="binary",
            ),
        )
        base_dir = tmp_path / "tasks"
        repo_path = tmp_path / "myrepo"
        result_path = write_task_dir(task, base_dir, repo_path)

        meta = json.loads((result_path / "metadata.json").read_text(encoding="utf-8"))
        assert meta["metadata"]["mcp_suite"] is None


# ---------------------------------------------------------------------------
# br7.4: mining yield improvements
# ---------------------------------------------------------------------------


class TestGhPrListCapturesBodyLabels:
    """``gh pr list`` should fetch body and labels inline."""

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_body_and_labels_populated_on_mergedpr(self, mock_run: object) -> None:
        gh_json = json.dumps(
            [
                {
                    "mergeCommit": {"oid": "aaa111"},
                    "title": "feat: add search",
                    "body": "Closes #42. Implements fuzzy search with ranking.",
                    "labels": [{"name": "feature"}, {"name": "search"}],
                },
                {
                    "mergeCommit": {"oid": "bbb222"},
                    "title": "fix: typo",
                    "body": "",
                    "labels": [],
                },
            ]
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=gh_json, stderr=""
        )
        source = RepoSource(host="github", owner="o", repo="r", remote_url="")

        prs = list_merged_prs(source, Path("/fake"), limit=10)

        assert len(prs) == 2
        # First PR has body and labels populated
        assert prs[0].sha == "aaa111"
        assert prs[0].body == "Closes #42. Implements fuzzy search with ranking."
        assert "feature" in prs[0].labels
        assert "search" in prs[0].labels
        # Second PR has empty body and no labels
        assert prs[1].body == ""
        assert prs[1].labels == ()

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_gh_pr_list_requests_body_and_labels(self, mock_run: object) -> None:
        """The gh pr list command must ask for body and labels fields."""
        gh_json = json.dumps(
            [{"mergeCommit": {"oid": "aaa111"}, "title": "t", "body": "", "labels": []}]
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=["gh"], returncode=0, stdout=gh_json, stderr=""
        )
        source = RepoSource(host="github", owner="o", repo="r", remote_url="")

        list_merged_prs(source, Path("/fake"), limit=5)

        # Find the gh pr list call among all subprocess.run invocations
        gh_calls = [
            c.args[0] for c in mock_run.call_args_list if c.args[0][0] == "gh"
        ]
        assert gh_calls, "Expected at least one gh invocation"
        cmd = gh_calls[0]
        assert "--json" in cmd
        json_idx = cmd.index("--json")
        fields = cmd[json_idx + 1]
        assert "body" in fields
        assert "labels" in fields
        assert "mergeCommit" in fields
        assert "title" in fields

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_git_log_fallback_has_empty_body(self, mock_run: object) -> None:
        """Non-gh path (git log fallback) populates empty body/labels."""
        log_output = "abc12345deadbeef Merge pull request #10 from fix/bug\n"
        mock_run.side_effect = _mock_git_log(log_output)
        source = RepoSource(host="gitlab", owner="o", repo="r", remote_url="")

        prs = list_merged_prs(source, Path("/fake"), limit=5)

        assert len(prs) == 1
        assert prs[0].body == ""
        assert prs[0].labels == ()


class TestApiTierFromPrBodyWithoutHashN:
    """When MergedPR carries a body inline, resolve_pr_metadata uses api tier."""

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_inline_body_skips_gh_pr_view(self, mock_run: object) -> None:
        """Passing pr_body inline to resolve_pr_metadata yields api tier without
        calling gh pr view (no #N needed in merge_title)."""
        call_commands: list[list[str]] = []

        def _side_effect(cmd, **kwargs):
            call_commands.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1, "", "should not be called")

        mock_run.side_effect = _side_effect
        source = RepoSource(host="github", owner="o", repo="r", remote_url="")

        meta = resolve_pr_metadata(
            "abc1234",
            Path("/fake"),
            source,
            "Squash merge of feature branch",
            pr_body="Fixes a rendering bug when users resize the viewport.",
            pr_labels=("bug", "ui"),
        )

        assert meta.source_tier == "api"
        assert meta.title == "Squash merge of feature branch"
        assert "rendering bug" in meta.body
        assert "bug" in meta.labels
        # gh pr view must NOT have been called
        assert not any(
            c[0] == "gh" and len(c) > 1 and c[1] == "pr" and c[2] == "view"
            for c in call_commands
            if len(c) >= 3
        )

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_empty_inline_body_falls_through_to_commit(self, mock_run: object) -> None:
        """Empty inline body → API tier skipped → commit message used."""
        commit_body = "Add caching\n\nReduce DB queries for hot path."

        def _side_effect(cmd, **kwargs):
            if cmd[0] == "gh":
                # gh pr view (no #N in title) shouldn't be called, but if it is
                # it should fail
                return subprocess.CompletedProcess(cmd, 1, "", "")
            return subprocess.CompletedProcess(cmd, 0, commit_body, "")

        mock_run.side_effect = _side_effect
        source = RepoSource(host="github", owner="o", repo="r", remote_url="")

        meta = resolve_pr_metadata(
            "abc1234",
            Path("/fake"),
            source,
            "Squash merge with no issue ref",
            pr_body="",
            pr_labels=(),
        )

        assert meta.source_tier == "commit_message"
        assert "DB queries" in meta.body


class TestBareWithFilesSurvivesSynthesized:
    """Merges with bare metadata but non-empty changed_files get synthesized body."""

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_no_source_and_no_body_synthesizes_description(
        self, mock_run: object
    ) -> None:
        """When source is None but files exist, task is created with synthesized
        description (merge_title + file list) instead of rejection."""
        files = ["src/auth.py", "tests/test_auth.py"]
        mock_run.side_effect = _mock_diff_name_only(files)

        result = extract_task_from_merge(
            "abc12345deadbeef",
            Path("/fake/myrepo"),
            merge_title="Fix auth flow",
        )

        assert result is not None
        task, pr_meta = result
        assert pr_meta.source_tier == "bare"
        # Synthesized body mentions changed files
        assert "src/auth.py" in pr_meta.body
        assert "src/auth.py" in task.metadata.description

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_bare_with_empty_files_still_rejected(self, mock_run: object) -> None:
        """When changed_files is empty, the merge is still rejected."""
        # Empty diff output
        mock_run.side_effect = _mock_diff_name_only([])

        result = extract_task_from_merge(
            "abc12345deadbeef",
            Path("/fake/myrepo"),
            merge_title="Empty merge",
        )
        assert result is None

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_resolve_pr_metadata_all_tiers_fail_but_files_present(
        self, mock_run: object
    ) -> None:
        """When all tiers fail during extract_task_from_merge with a local source
        and merge_title + files, a task is produced with synthesized description."""
        files = ["pkg/scheduler/queue.go", "pkg/scheduler/queue_test.go"]

        def _side_effect(cmd, **kwargs):
            if "diff" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "\n".join(files), "")
            # git log for commit body — fail (bare)
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", "")
            return subprocess.CompletedProcess(cmd, 1, "", "")

        mock_run.side_effect = _side_effect
        source = RepoSource(host="local", owner="", repo="r", remote_url="")

        result = extract_task_from_merge(
            "abc12345deadbeef",
            Path("/fake/r"),
            source=source,
            merge_title="Refactor scheduler queue",
        )

        assert result is not None
        task, pr_meta = result
        assert pr_meta.source_tier == "bare"
        # Synthesis uses merge_title or file list
        assert (
            "queue" in task.metadata.description.lower()
            or "scheduler" in task.metadata.description.lower()
        )


class TestMineTasksRespectsMinQualityFlag:
    """mine_tasks must honor the min_quality parameter end-to-end."""

    @patch("codeprobe.mining.extractor.subprocess.run")
    @patch("codeprobe.mining.sources.subprocess.run")
    def test_min_quality_zero_includes_low_score_tasks(
        self, mock_sources_run: object, mock_extractor_run: object
    ) -> None:
        """With min_quality=0.0, tasks passing hard gates are kept even with low
        quality scores."""
        merge_log = "aaaa1111bbbb2222 chore: cleanup\n"
        diff_files = "src/cleanup.py\ntests/test_other.py\n"
        # Commit body with no issue ref and no test/source overlap → low score
        commit_body = "chore: cleanup\n\nRemoved some dead code."

        mock_sources_run.side_effect = _mock_git_remote(
            "https://github.com/o/r.git\n"
        )

        def _extractor_side_effect(cmd, **kwargs):
            if cmd[0] == "gh":
                return subprocess.CompletedProcess(cmd, 1, "", "")
            if "log" in cmd:
                if "--merges" in cmd:
                    return subprocess.CompletedProcess(cmd, 0, merge_log, "")
                return subprocess.CompletedProcess(cmd, 0, commit_body, "")
            if "diff" in cmd:
                return subprocess.CompletedProcess(cmd, 0, diff_files, "")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        mock_extractor_run.side_effect = _extractor_side_effect

        # Default min_quality=0.5 filters this task out
        default_result = mine_tasks(Path("/fake/repo"), count=5)
        assert default_result.tasks == []

        # min_quality=0.0 admits it
        open_result = mine_tasks(Path("/fake/repo"), count=5, min_quality=0.0)
        assert len(open_result.tasks) == 1
        assert open_result.tasks[0].id == "aaaa1111"


class TestCliMinQualityOption:
    """The --min-quality CLI flag must be wired through to mine_tasks."""

    @patch("codeprobe.cli.mine_cmd._dispatch_sdlc")
    @patch("codeprobe.cli.mine_cmd._resolve_repo_path")
    def test_min_quality_flag_passed_to_dispatch(
        self, mock_resolve, mock_dispatch, tmp_path: Path
    ) -> None:
        """--min-quality 0.2 should reach _dispatch_sdlc as the same value."""
        from click.testing import CliRunner

        from codeprobe.cli import main

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        mock_resolve.return_value = repo

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "mine",
                str(repo),
                "--goal",
                "quality",
                "--min-quality",
                "0.25",
                "--no-interactive",
            ],
        )

        assert result.exit_code == 0, result.output
        assert mock_dispatch.called
        kwargs = mock_dispatch.call_args.kwargs
        assert kwargs.get("min_quality") == pytest.approx(0.25)
