"""Tests for --cross-repo CLI option in codeprobe mine."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from codeprobe.cli import main


class TestCrossRepoMutualExclusion:
    """--cross-repo and --org-scale must not be used together."""

    def test_cross_repo_and_org_scale_raises_usage_error(self, tmp_path):
        """Using both --cross-repo and --org-scale should fail with UsageError."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "mine",
                str(repo),
                "--cross-repo",
                "/some/other/repo",
                "--org-scale",
                "--no-interactive",
            ],
        )
        assert result.exit_code != 0
        assert "Cannot use --cross-repo with --org-scale" in result.output


class TestCrossRepoDefaultGoal:
    """--cross-repo without --goal should default to mcp."""

    @patch("codeprobe.cli.mine_cmd._dispatch_cross_repo")
    @patch("codeprobe.cli.mine_cmd._resolve_repo_path")
    def test_defaults_to_mcp_goal(self, mock_resolve, mock_dispatch, tmp_path):
        """When --cross-repo is used without --goal, goal defaults to mcp."""
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
                "--cross-repo",
                "/some/secondary",
                "--no-interactive",
            ],
        )
        # Should print the default message
        assert "Defaulting to --goal mcp for cross-repo mining" in result.output


class TestCrossRepoResolverFallback:
    """When no SG auth, should fall back to RipgrepResolver with warning."""

    @patch("codeprobe.mining.multi_repo.mine_tasks_multi")
    @patch("codeprobe.cli.mine_cmd._resolve_repo_path")
    def test_fallback_to_ripgrep_warning(
        self, mock_resolve, mock_multi, tmp_path, monkeypatch
    ):
        """Without SRC_ACCESS_TOKEN, should warn about fallback."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        secondary = tmp_path / "secondary"
        secondary.mkdir()
        (secondary / ".git").mkdir()
        mock_resolve.return_value = repo

        # Ensure no SG token
        monkeypatch.delenv("SRC_ACCESS_TOKEN", raising=False)

        # Return empty result to avoid further processing
        from codeprobe.mining.multi_repo import MultiRepoMineResult

        mock_multi.return_value = MultiRepoMineResult(tasks=[], ground_truth_files={})

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "mine",
                str(repo),
                "--cross-repo",
                str(secondary),
                "--no-interactive",
            ],
        )
        # Warning goes to stderr; CliRunner mixes stdout+stderr by default
        combined = result.output + (result.stderr or "")
        assert "falling back to ripgrep" in combined.lower()


class TestMineUrlValidation:
    """Pre-clone URL-shape checks emit actionable 'not a valid git URL' errors."""

    def test_rejects_non_git_scheme(self, tmp_path):
        """ftp:// and similar schemes should be rejected before clone."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["mine", "ftp://example.com/repo.git", "--no-interactive"],
        )
        assert result.exit_code == 2
        assert "not a valid git URL" in result.output
        assert "scheme 'ftp'" in result.output
        # Must not attempt to clone.
        assert "Cloning" not in result.output

    def test_rejects_url_without_path(self, tmp_path):
        """https://host with no repo path should be rejected before clone."""
        runner = CliRunner()
        result = runner.invoke(
            main,
            ["mine", "https://example.com", "--no-interactive"],
        )
        assert result.exit_code == 2
        assert "not a valid git URL" in result.output
        assert "missing repository path" in result.output
        assert "Cloning" not in result.output

    def test_accepts_owner_repo_shorthand(self, tmp_path, monkeypatch):
        """owner/repo shorthand should still route through clone, not URL validator."""
        from codeprobe.cli import mine_cmd

        called = {}

        def fake_clone(url: str):
            called["url"] = url
            raise click.UsageError("stub")  # noqa: F821  # click imported below

        import click

        monkeypatch.setattr(mine_cmd, "_clone_repo", fake_clone)

        runner = CliRunner()
        result = runner.invoke(
            main, ["mine", "octocat/hello-world", "--no-interactive"]
        )
        # We expect the stub clone to be reached (shorthand accepted).
        assert called.get("url") == "octocat/hello-world"
        assert result.exit_code == 2  # our stub raised
        assert "stub" in result.output


class TestCrossRepoDispatch:
    """Verify _dispatch_cross_repo is called with correct args."""

    @patch("codeprobe.cli.mine_cmd._dispatch_cross_repo")
    @patch("codeprobe.cli.mine_cmd._resolve_repo_path")
    def test_dispatch_called_with_correct_args(
        self, mock_resolve, mock_dispatch, tmp_path
    ):
        """--cross-repo should invoke _dispatch_cross_repo with secondary paths."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        mock_resolve.return_value = repo

        runner = CliRunner()
        runner.invoke(
            main,
            [
                "mine",
                str(repo),
                "--cross-repo",
                "/path/to/secondary",
                "--goal",
                "mcp",
                "--no-interactive",
                "--count",
                "3",
            ],
        )
        mock_dispatch.assert_called_once()
        call_kwargs = mock_dispatch.call_args[1]
        assert call_kwargs["primary"] == repo
        assert call_kwargs["cross_repo"] == ("/path/to/secondary",)
        assert call_kwargs["count"] == 3
