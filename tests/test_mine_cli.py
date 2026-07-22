"""Tests for --cross-repo CLI option in codeprobe mine."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
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

        # Ensure no SG token in any accepted env var, and isolate the auth cache
        from codeprobe.mining import sg_auth

        for name in sg_auth._ACCEPTED_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

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

    def test_rejects_bare_owner_repo_shorthand(self, tmp_path, monkeypatch):
        """Bare owner/repo is ambiguous: exit 2 naming github:owner/repo, no clone."""
        from codeprobe.cli import mine_cmd

        def fake_clone(url: str):
            raise AssertionError(f"_clone_repo must not be called (got {url!r})")

        monkeypatch.setattr(mine_cmd, "_clone_repo", fake_clone)

        runner = CliRunner()
        result = runner.invoke(
            main, ["mine", "octocat/hello-world", "--no-interactive"]
        )
        assert result.exit_code == 2
        assert "github:octocat/hello-world" in result.output
        assert "not an existing local path" in result.output
        assert "Cloning" not in result.output

    def test_github_prefix_shorthand_clones(self, tmp_path, monkeypatch):
        """github:owner/repo routes to clone with the normalized https URL."""
        import click

        from codeprobe.cli import mine_cmd

        called = {}

        def fake_clone(url: str):
            called["url"] = url
            raise click.UsageError("stub")

        monkeypatch.setattr(mine_cmd, "_clone_repo", fake_clone)

        runner = CliRunner()
        result = runner.invoke(
            main, ["mine", "github:octocat/hello-world", "--no-interactive"]
        )
        assert called.get("url") == "https://github.com/octocat/hello-world.git"
        assert result.exit_code == 2  # our stub raised
        assert "stub" in result.output

    def test_relative_path_never_cloned(self, monkeypatch):
        """./minerepo resolves as a local path; git clone is never invoked."""
        from pathlib import Path

        from codeprobe.cli import mine_cmd

        def fake_clone(url: str):
            raise AssertionError(f"_clone_repo must not be called (got {url!r})")

        monkeypatch.setattr(mine_cmd, "_clone_repo", fake_clone)

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("minerepo/.git").mkdir(parents=True)
            result = runner.invoke(main, ["mine", "./minerepo", "--no-interactive"])
        assert "Cloning" not in result.output
        assert "not an existing local path" not in result.output

    def test_local_dir_wins_over_github_name_collision(self, monkeypatch):
        """A local work/api dir wins over the identically named GitHub repo."""
        from pathlib import Path

        from codeprobe.cli import mine_cmd

        def fake_clone(url: str):
            raise AssertionError(f"_clone_repo must not be called (got {url!r})")

        monkeypatch.setattr(mine_cmd, "_clone_repo", fake_clone)

        runner = CliRunner()
        with runner.isolated_filesystem():
            Path("work/api/.git").mkdir(parents=True)
            result = runner.invoke(main, ["mine", "work/api", "--no-interactive"])
        assert "Cloning" not in result.output
        assert "not an existing local path" not in result.output

    def test_nonexistent_relative_path_errors_as_path(self, monkeypatch):
        """./does-not-exist gets a path error, never a clone attempt."""
        from codeprobe.cli import mine_cmd

        def fake_clone(url: str):
            raise AssertionError(f"_clone_repo must not be called (got {url!r})")

        monkeypatch.setattr(mine_cmd, "_clone_repo", fake_clone)

        runner = CliRunner()
        with runner.isolated_filesystem():
            result = runner.invoke(
                main, ["mine", "./does-not-exist", "--no-interactive"]
            )
        assert result.exit_code == 2
        assert "Path does not exist" in result.output
        assert "Cloning" not in result.output


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


def _make_probe_repo(base: Path) -> Path:
    """Create a git repo with enough Python symbols to mine probes from."""
    repo = base / "proberepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "test"], cwd=repo, check=True)
    (repo / "calc.py").write_text(
        '"""Small calculator module."""\n'
        "\n"
        "\n"
        "def add(a: int, b: int) -> int:\n"
        '    """Return the sum of a and b."""\n'
        "    return a + b\n"
        "\n"
        "\n"
        "def subtract(a: int, b: int) -> int:\n"
        '    """Return the difference of a and b."""\n'
        "    return a - b\n"
        "\n"
        "\n"
        "def multiply(a: int, b: int) -> int:\n"
        '    """Return the product of a and b."""\n'
        "    return a * b\n"
        "\n"
        "\n"
        "class Calculator:\n"
        '    """Stateful calculator."""\n'
        "\n"
        "    def __init__(self) -> None:\n"
        "        self.total = 0\n"
        "\n"
        "    def accumulate(self, value: int) -> int:\n"
        '        """Add value to the running total."""\n'
        "        self.total += value\n"
        "        return self.total\n"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _mine_probes(repo: Path, *extra_args: str):
    """Invoke ``codeprobe mine`` on the probe fixture repo."""
    return CliRunner().invoke(
        main,
        [
            "mine",
            str(repo),
            "--task-type",
            "micro_probe",
            "--no-interactive",
            "--no-llm",
            *extra_args,
        ],
    )


class TestMineAutoCreatesExperiment:
    """mine auto-creates .codeprobe/experiment.json when none exists."""

    def test_mine_creates_default_experiment_with_task_ids(self, tmp_path):
        """Fresh repo: mine writes a default experiment listing mined ids."""
        repo = _make_probe_repo(tmp_path)

        result = _mine_probes(repo, "--json")

        assert result.exit_code == 0, result.output
        exp_json = repo / ".codeprobe" / "experiment.json"
        assert exp_json.is_file()
        data = json.loads(exp_json.read_text())
        assert data["name"] == "default"
        mined = sorted(
            d.name
            for d in (repo / ".codeprobe" / "tasks").iterdir()
            if d.is_dir() and (d / "instruction.md").is_file()
        )
        assert mined  # the fixture must actually yield tasks
        assert data["task_ids"] == mined

        envelope = json.loads(
            [ln for ln in result.output.splitlines() if ln.strip()][-1]
        )
        assert envelope["data"]["experiment_created"] is True
        assert envelope["data"]["experiment_dir"] == str(repo / ".codeprobe")

    def test_mine_updates_existing_direct_experiment(self, tmp_path):
        """Regression: a direct .codeprobe/experiment.json gets task_ids."""
        repo = _make_probe_repo(tmp_path)
        init_result = CliRunner().invoke(
            main,
            ["experiment", "init", str(repo), "--name", "my-exp", "--non-interactive"],
        )
        assert init_result.exit_code == 0, init_result.output

        result = _mine_probes(repo, "--json")

        assert result.exit_code == 0, result.output
        data = json.loads((repo / ".codeprobe" / "experiment.json").read_text())
        assert data["name"] == "my-exp"  # existing experiment preserved
        assert data["task_ids"]  # mined ids recorded in the direct location

        envelope = json.loads(
            [ln for ln in result.output.splitlines() if ln.strip()][-1]
        )
        assert envelope["data"]["experiment_created"] is False
        assert envelope["data"]["experiment_dir"] == str(repo / ".codeprobe")

    def test_mine_leaves_ambiguous_experiments_untouched(self, tmp_path):
        """Two named experiment subdirs: mine neither creates nor modifies."""
        from codeprobe.core.experiment import create_experiment_dir, load_experiment
        from codeprobe.models.experiment import Experiment

        repo = _make_probe_repo(tmp_path)
        codeprobe_dir = repo / ".codeprobe"
        for name in ("exp-a", "exp-b"):
            create_experiment_dir(codeprobe_dir, Experiment(name=name))

        result = _mine_probes(repo)

        assert result.exit_code == 0, result.output
        assert not (codeprobe_dir / "experiment.json").exists()
        for name in ("exp-a", "exp-b"):
            assert load_experiment(codeprobe_dir / name).task_ids == ()

    def test_mine_then_run_dry_run_finds_experiment(self, tmp_path):
        """Quick Start regression: mine -> run --dry-run, no NO_EXPERIMENT."""
        repo = _make_probe_repo(tmp_path)

        mine_result = _mine_probes(repo)
        assert mine_result.exit_code == 0, mine_result.output

        run_result = CliRunner().invoke(main, ["run", str(repo), "--dry-run"])

        combined = run_result.output + (run_result.stderr or "")
        assert "NO_EXPERIMENT" not in combined
        assert run_result.exit_code == 0, combined

    def test_record_task_ids_echoes_creation_in_pretty_mode(self, tmp_path, capsys):
        """Direct call without a resolved output mode echoes the creation."""
        from codeprobe.cli.mine_cmd import _record_task_ids_in_experiment
        from codeprobe.core.experiment import load_experiment

        _record_task_ids_in_experiment(tmp_path, ["t2", "t1"])

        codeprobe_dir = tmp_path / ".codeprobe"
        assert load_experiment(codeprobe_dir).task_ids == ("t1", "t2")
        captured = capsys.readouterr()
        assert (
            "Created default experiment at .codeprobe/experiment.json"
            in captured.out
        )
