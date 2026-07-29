"""Tests for --cross-repo CLI option in codeprobe mine."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.models.task import Task, TaskMetadata, TaskVerification


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


class TestOrgScaleNextSteps:
    def test_run_command_quotes_repo_and_suite_paths(self, tmp_path: Path) -> None:
        from io import StringIO

        from codeprobe.cli.mine_cmd import _show_org_scale_results

        task = Task(
            id="org-001",
            repo="repo",
            metadata=TaskMetadata(
                name="org-task",
                difficulty="medium",
                category="migration-inventory",
                org_scale=True,
            ),
            verification=TaskVerification(
                oracle_type="file_list",
                oracle_answer=("a.py",),
            ),
        )
        repo = tmp_path / "repo with spaces"
        output_root = tmp_path / "output with spaces"
        tasks_dir = output_root / "tasks"
        out_dir = tmp_path / "out with spaces"
        repo.mkdir()
        tasks_dir.mkdir(parents=True)
        out_dir.mkdir()

        buf = StringIO()
        with patch(
            "click.echo", side_effect=lambda msg="", **kw: buf.write(msg + "\n")
        ):
            _show_org_scale_results([task], tasks_dir, repo, out_dir=out_dir)

        suite = output_root / "suite.toml"
        assert (
            f"codeprobe run {shlex.quote(str(repo))} "
            f"--config {shlex.quote(str(out_dir))} "
            f"--suite {shlex.quote(str(suite))} --agent claude"
        ) in buf.getvalue()
        assert f"codeprobe run {shlex.quote(str(out_dir))}" not in buf.getvalue()


# ---------------------------------------------------------------------------
# Ctrl-C recovery + --resume (codeprobe-f7rl.14)
# ---------------------------------------------------------------------------


def _make_sdlc_repo(base: Path, *, untested_merges: int = 0) -> Path:
    """Create a git repo with merged feature branches that include tests.

    *untested_merges* adds merges that touch two source files but carry no
    test files — the extractor rejects those with the ``extraction`` counter
    (no test command can be mined), independent of any goal-profile
    ``min_files`` value.
    """
    repo = base / "sdlcrepo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "test")
    (repo / "README.md").write_text("sdlc fixture\n")
    _git("add", ".")
    _git("commit", "-qm", "chore: init")

    for i in (1, 2):
        branch = f"feat/{i}"
        _git("checkout", "-q", "-b", branch)
        (repo / f"feature_{i}.py").write_text(
            f'"""Feature {i}."""\n\n\ndef feature_{i}() -> int:\n    return {i}\n'
        )
        (repo / "tests").mkdir(exist_ok=True)
        (repo / "tests" / f"test_feature_{i}.py").write_text(
            f"from feature_{i} import feature_{i}\n\n\n"
            f"def test_feature_{i}():\n    assert feature_{i}() == {i}\n"
        )
        _git("add", ".")
        _git(
            "commit",
            "-qm",
            f"feat: add feature {i}\n\nImplements feature {i} with unit tests.",
        )
        _git("checkout", "-q", "main")
        _git(
            "merge",
            "--no-ff",
            "-q",
            "-m",
            f"Merge PR #{i}: add feature {i}\n\nAdds feature {i} and its tests.",
            branch,
        )
    for i in range(untested_merges):
        branch = f"notes/{i}"
        _git("checkout", "-q", "-b", branch)
        (repo / f"notes_a_{i}.py").write_text(f"NOTES_A = {i}\n")
        (repo / f"notes_b_{i}.py").write_text(f"NOTES_B = {i}\n")
        _git("add", ".")
        _git("commit", "-qm", f"docs: notes {i}\n\nAdds untested notes modules.")
        _git("checkout", "-q", "main")
        _git(
            "merge",
            "--no-ff",
            "-q",
            "-m",
            f"Merge PR #{i + 10}: notes {i}\n\nAdds notes modules without tests.",
            branch,
        )
    return repo


class TestMineInterruptRecovery:
    """Ctrl-C must preserve partial output and print a real recovery command."""

    def test_ctrl_c_preserves_partial_output_and_prints_real_resume(
        self, tmp_path, monkeypatch
    ):
        repo = _make_probe_repo(tmp_path)
        tasks_dir = repo / ".codeprobe" / "tasks"

        def _boom(**kwargs):
            partial = tasks_dir / "task-partial"
            partial.mkdir(parents=True)
            (partial / "instruction.md").write_text("partial\n")
            monkeypatch.setattr(
                "codeprobe.cli.mine_cmd._CURRENT_TASKS_DIR", tasks_dir
            )
            raise KeyboardInterrupt

        with patch(
            "codeprobe.cli.mine_cmd._dispatch_by_task_type", side_effect=_boom
        ):
            result = CliRunner().invoke(
                main, ["mine", str(repo), "--no-interactive", "--json"]
            )

        assert result.exit_code == 130, result.output
        # Partial output survives — never rmtree'd (locked decision 3).
        assert (tasks_dir / "task-partial" / "instruction.md").is_file()
        envelope = json.loads(
            [ln for ln in result.output.splitlines() if ln.strip()][-1]
        )
        assert envelope["error"]["code"] == "INTERRUPTED"
        # The printed recovery command is a real invocation of a real flag.
        assert (
            envelope["error"]["diagnose_cmd"] == f"codeprobe mine {repo} --resume"
        )

    def test_mine_help_lists_resume(self):
        result = CliRunner().invoke(main, ["mine", "--help"])
        assert result.exit_code == 0
        assert "--resume" in result.output


class TestMineResumeFlag:
    """--resume semantics: fresh-state warning, SDLC mining, rejections."""

    def test_resume_without_prior_state_warns_and_mines(
        self, tmp_path, monkeypatch
    ):
        state_root = tmp_path / "state"
        state_root.mkdir()
        monkeypatch.setenv("CODEPROBE_STATE_ROOT", str(state_root))
        repo = _make_sdlc_repo(tmp_path)

        result = CliRunner().invoke(
            main,
            [
                "mine",
                str(repo),
                "--goal",
                "quality",
                "--resume",
                "--no-interactive",
                "--no-llm",
                "--min-quality",
                "0",
                "--narrative-source",
                "commits",
                "--json",
            ],
        )

        combined = result.output + (result.stderr or "")
        assert result.exit_code == 0, combined
        assert "no prior mining state" in combined
        envelope = json.loads(
            [ln for ln in result.output.splitlines() if ln.strip()][-1]
        )
        assert envelope["data"]["task_count"] >= 1

    def test_resume_rejected_with_org_scale(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()

        result = CliRunner().invoke(
            main,
            ["mine", str(repo), "--org-scale", "--resume", "--no-interactive"],
        )
        combined = result.output + (result.stderr or "")
        assert result.exit_code != 0
        assert "Cannot use --resume with --org-scale" in combined

    def test_resume_rejected_for_probe_task_type(self, tmp_path, monkeypatch):
        state_root = tmp_path / "state"
        state_root.mkdir()
        monkeypatch.setenv("CODEPROBE_STATE_ROOT", str(state_root))
        repo = _make_probe_repo(tmp_path)

        result = CliRunner().invoke(
            main,
            [
                "mine",
                str(repo),
                "--task-type",
                "micro_probe",
                "--resume",
                "--no-interactive",
            ],
        )
        combined = result.output + (result.stderr or "")
        assert result.exit_code != 0
        assert "Cannot use --resume with task type micro_probe" in combined


# ---------------------------------------------------------------------------
# Zero-yield / shortfall envelope honesty (codeprobe-f7rl.16)
# ---------------------------------------------------------------------------


def _mine_sdlc_envelope(repo: Path, *extra_args: str):
    """Run an SDLC mine in --json mode and return (result, envelope)."""
    result = CliRunner().invoke(
        main,
        [
            "mine",
            str(repo),
            "--goal",
            "quality",
            "--no-interactive",
            "--no-llm",
            "--narrative-source",
            "commits",
            "--json",
            *extra_args,
        ],
    )
    lines = [ln for ln in result.output.splitlines() if ln.strip()]
    envelope = json.loads(lines[-1]) if lines else None
    return result, envelope


class TestMineShortfallEnvelope:
    """Zero-yield and shortfall mines must be machine-distinguishable."""

    def test_zero_yield_carries_rejections_warning_and_next_steps(
        self, tmp_path, monkeypatch
    ):
        """All candidates filtered: ok stays true but remediation is emitted."""
        state_root = tmp_path / "state"
        state_root.mkdir()
        monkeypatch.setenv("CODEPROBE_STATE_ROOT", str(state_root))
        repo = _make_sdlc_repo(tmp_path, untested_merges=1)

        result, envelope = _mine_sdlc_envelope(repo, "--min-quality", "1.0")

        assert result.exit_code == 0, result.output
        assert envelope["ok"] is True
        assert envelope["data"]["task_count"] == 0
        # Both tested PRs score 0.5 < 1.0 (quality), the untested merge has
        # no minable test command (extraction): total 3 filtered.
        rejections = envelope["data"]["rejections"]
        assert rejections["quality"] == 2
        assert rejections["extraction"] == 1
        assert rejections["total"] == 3
        warning_codes = [w["code"] for w in envelope["warnings"]]
        assert "MINE_SHORTFALL" in warning_codes
        # Dominant filter is quality — the remediation command must be
        # executable and carry the dominant filter's flag.
        assert envelope["next_steps"], "zero-yield envelope must carry next_steps"
        first = envelope["next_steps"][0]
        assert "--min-quality" in first["command"]
        assert first["command"].startswith(f"codeprobe mine {repo}")

    def test_partial_shortfall_carries_rejections_and_next_steps(
        self, tmp_path, monkeypatch
    ):
        """Mined < requested with filtering: rejections + next_steps present."""
        state_root = tmp_path / "state"
        state_root.mkdir()
        monkeypatch.setenv("CODEPROBE_STATE_ROOT", str(state_root))
        repo = _make_sdlc_repo(tmp_path, untested_merges=1)

        result, envelope = _mine_sdlc_envelope(
            repo, "--count", "5", "--min-quality", "0"
        )

        assert result.exit_code == 0, result.output
        assert envelope["ok"] is True
        assert envelope["data"]["task_count"] == 2
        rejections = envelope["data"]["rejections"]
        assert rejections["total"] >= 1
        warning_codes = [w["code"] for w in envelope["warnings"]]
        assert "MINE_SHORTFALL" in warning_codes
        # The remediation is an executable codeprobe mine/--task-type command
        # derived from whichever filter dominated.
        assert envelope["next_steps"]
        assert envelope["next_steps"][0]["command"].startswith("codeprobe mine ")

    def test_full_yield_keeps_next_steps_empty_and_no_rejections_key(
        self, tmp_path, monkeypatch
    ):
        """Request met: the envelope stays indistinguishable from before."""
        state_root = tmp_path / "state"
        state_root.mkdir()
        monkeypatch.setenv("CODEPROBE_STATE_ROOT", str(state_root))
        repo = _make_sdlc_repo(tmp_path, untested_merges=1)

        result, envelope = _mine_sdlc_envelope(
            repo, "--count", "2", "--min-quality", "0"
        )

        assert result.exit_code == 0, result.output
        assert envelope["ok"] is True
        assert envelope["data"]["task_count"] == 2
        assert "rejections" not in envelope["data"]
        assert envelope["next_steps"] == []
        warning_codes = [w["code"] for w in envelope["warnings"]]
        assert "MINE_SHORTFALL" not in warning_codes


# ---------------------------------------------------------------------------
# LLM spend metering — summary line + envelope field (codeprobe-f7rl.37)
# ---------------------------------------------------------------------------


class TestMineLLMSpendReporting:
    """Every mine run reports metered LLM spend; --no-llm provably reports 0."""

    @pytest.fixture(autouse=True)
    def _clean_ledger(self):
        from codeprobe.core.llm import reset_llm_spend

        reset_llm_spend()
        yield
        reset_llm_spend()

    def test_no_llm_summary_reports_zero_calls(self, tmp_path):
        """--no-llm: pretty summary says 'LLM calls: 0', envelope agrees."""
        repo = _make_probe_repo(tmp_path)

        pretty = _mine_probes(repo)
        assert pretty.exit_code == 0, pretty.output
        assert "LLM calls:       0" in pretty.output

        enveloped = _mine_probes(repo, "--json")
        assert enveloped.exit_code == 0, enveloped.output
        envelope = json.loads(
            [ln for ln in enveloped.output.splitlines() if ln.strip()][-1]
        )
        spend = envelope["data"]["llm_spend"]
        assert spend["calls"] == 0
        assert spend["input_tokens"] == 0
        assert spend["output_tokens"] == 0
        assert spend["cost_usd"] == 0.0
        assert spend["cost_unknown_calls"] == 0
        assert spend["cost_source"] == "calculated"

    def test_mine_summary_and_envelope_carry_llm_spend(self, tmp_path, monkeypatch):
        """Calls made during mining land in the summary line and envelope."""
        import codeprobe.core.llm as core_llm
        import codeprobe.probe.generator as probe_generator

        stub_response = core_llm.LLMResponse(
            text="ok",
            input_tokens=1_000_000,
            output_tokens=1_000_000,
            cost_usd=None,  # forces tokens x CLAUDE_PRICING (haiku: $6.00/call)
            model="claude-haiku-4-5-20251001",
            backend="stub",
        )

        class _Stub:
            name = "stub"

            def available(self) -> bool:
                return True

            def call(self, request):
                return stub_response

        monkeypatch.setattr(core_llm, "_resolve_backend", lambda: _Stub())

        real_generate = probe_generator.generate_probes

        def metered_generate(repo_root, count=5, **kwargs):
            for _ in range(3):
                core_llm.call_llm(core_llm.LLMRequest(prompt="meter me"))
            return real_generate(repo_root, count=count, **kwargs)

        monkeypatch.setattr(probe_generator, "generate_probes", metered_generate)

        repo = _make_probe_repo(tmp_path)
        args = ["mine", str(repo), "--task-type", "micro_probe", "--no-interactive"]

        pretty = CliRunner().invoke(main, args)
        assert pretty.exit_code == 0, pretty.output
        assert (
            "LLM calls:       3 (in=3000000 out=3000000 tokens, "
            "~$18.0000 calculated)" in pretty.output
        )

        enveloped = CliRunner().invoke(main, [*args, "--json"])
        assert enveloped.exit_code == 0, enveloped.output
        envelope = json.loads(
            [ln for ln in enveloped.output.splitlines() if ln.strip()][-1]
        )
        assert envelope["data"]["llm_spend"] == {
            "calls": 3,
            "input_tokens": 3_000_000,
            "output_tokens": 3_000_000,
            "cost_usd": 18.0,
            "cost_unknown_calls": 0,
            "cost_source": "calculated",
        }
