"""Tests for the task mining module."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

from codeprobe.mining.extractor import (
    PRMetadata,
    _build_test_command,
    _format_task_description,
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

    def test_write_enriched_instruction(self, tmp_path: Path) -> None:
        """Enriched multi-line description appears in instruction.md."""
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
        assert "Fix auth token refresh" in instruction
        assert "silent logout" in instruction
        assert "Labels: bug, auth" in instruction
        assert "Reproduce the changes" in instruction


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
        """PR with issue ref, description body, and targeted tests scores 1.0."""
        score = score_pr_quality(
            title="Fix auth redirect (#42)",
            body="Tokens were not refreshed on 401 responses. This caused silent logout.",
            changed_files=["src/auth.py", "src/middleware.py", "tests/test_auth.py"],
            test_files=["tests/test_auth.py"],
        )
        assert score == pytest.approx(1.0)

    def test_no_issue_ref(self) -> None:
        """PR without issue reference loses that signal."""
        score = score_pr_quality(
            title="Fix auth redirect",
            body="Tokens were not refreshed on 401 responses.",
            changed_files=["src/auth.py", "tests/test_auth.py"],
            test_files=["tests/test_auth.py"],
        )
        assert score == pytest.approx(2 / 3, abs=0.01)

    def test_empty_body(self) -> None:
        """PR with empty body loses that signal."""
        score = score_pr_quality(
            title="Fix auth redirect (#42)",
            body="",
            changed_files=["src/auth.py", "tests/test_auth.py"],
            test_files=["tests/test_auth.py"],
        )
        assert score == pytest.approx(2 / 3, abs=0.01)

    def test_unrelated_tests(self) -> None:
        """Test files that don't share source dirs get no test coverage signal."""
        score = score_pr_quality(
            title="Fix auth redirect (#42)",
            body="Good description of the problem and fix.",
            changed_files=["src/auth.py", "tests/test_unrelated.py"],
            test_files=["tests/test_unrelated.py"],
        )
        # Issue ref + body present, but test file dir (tests/) doesn't overlap src dir (src/)
        assert score == pytest.approx(2 / 3, abs=0.01)

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
        # Issue ref (in body) + body + targeted tests
        assert score == pytest.approx(1.0)

    def test_short_body_not_meaningful(self) -> None:
        """Body under 20 chars is not considered meaningful."""
        score = score_pr_quality(
            title="Fix bug (#1)",
            body="short",
            changed_files=["src/auth.py", "tests/test_auth.py"],
            test_files=["tests/test_auth.py"],
        )
        # Issue ref + targeted tests, but body too short
        assert score == pytest.approx(2 / 3, abs=0.01)


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

        task = extract_task_from_merge(
            "abc12345deadbeef",
            Path("/fake/myrepo"),
            source=source,
            merge_title="Fix auth",
        )

        assert task is not None
        assert "expired silently" in task.metadata.description

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_without_source_bare_description(self, mock_run: object) -> None:
        """Backward compat: no source → bare description like before."""
        files = ["src/auth.py", "tests/test_auth.py"]
        mock_run.side_effect = _mock_diff_name_only(files)

        task = extract_task_from_merge("abc12345deadbeef", Path("/fake/myrepo"))

        assert task is not None
        assert "abc12345" in task.metadata.description
