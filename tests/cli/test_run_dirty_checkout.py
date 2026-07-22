"""Tests for the dirty-checkout hard refusal on ``codeprobe run`` (codeprobe-f7rl.1).

Trial worktrees are created detached from HEAD (``codeprobe.core.isolation``),
so uncommitted changes in the customer checkout are invisible to every trial.
``codeprobe run`` must refuse a dirty checkout with DIRTY_CHECKOUT before any
adapter is resolved, unless ``--allow-dirty`` is passed. Codeprobe-owned
artifacts (``.codeprobe/``, ``.codeprobe-worktrees*``, ``runs/``, experiment
dirs) never count as dirt. A non-git directory is NOT_A_GIT_REPO because
worktree isolation cannot function at all.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codeprobe.cli import run_cmd as run_cmd_mod
from codeprobe.cli.errors import DiagnosticError, PrescriptiveError
from tests.conftest import FakeAdapter


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True
    )


def _init_git_repo(repo: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.email", "r@example.com")
    _git(repo, "config", "user.name", "r")
    (repo / "README.md").write_text("seed\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "seed")


def _setup_experiment(root: Path) -> Path:
    """Create a minimal one-task experiment under <root>/.codeprobe/exp."""
    exp_dir = root / ".codeprobe" / "exp"
    task_dir = exp_dir / "tasks" / "task-001"
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("Do stuff.", encoding="utf-8")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    test_sh = tests_dir / "test.sh"
    test_sh.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    test_sh.chmod(0o755)
    experiment_json = {
        "name": "exp",
        "description": "dirty-checkout test",
        "tasks_dir": "tasks",
        "task_ids": ["task-001"],
        "configs": [
            {
                "label": "baseline",
                "agent": "fake",
                "model": None,
                "extra": {"timeout_seconds": 60},
            }
        ],
    }
    (exp_dir / "experiment.json").write_text(
        json.dumps(experiment_json), encoding="utf-8"
    )
    return exp_dir


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    _setup_experiment(repo)
    return repo


def _assert_refused_before_adapter(repo: Path) -> PrescriptiveError:
    """Run run_eval against *repo* and assert the DIRTY_CHECKOUT refusal
    fires before the adapter registry or executor is reached."""
    exp_dir = repo / ".codeprobe" / "exp"
    with (
        patch.object(run_cmd_mod, "resolve") as mock_resolve,
        patch.object(run_cmd_mod, "execute_config") as mock_execute,
    ):
        with pytest.raises(PrescriptiveError) as exc_info:
            run_cmd_mod.run_eval(
                str(exp_dir), agent="fake", parallel=1, quiet=True,
                force_plain=True,
            )
    assert not mock_resolve.called, "adapter registry must not be reached"
    assert not mock_execute.called, "execute_config must not be reached"
    err = exc_info.value
    assert err.code == "DIRTY_CHECKOUT"
    assert err.next_try_flag == "--allow-dirty"
    return err


class TestDirtyRefusal:
    def test_unstaged_edit_to_tracked_file_refused(self, repo: Path) -> None:
        (repo / "README.md").write_text("modified\n")

        err = _assert_refused_before_adapter(repo)

        assert "README.md" in err.detail["dirty_paths"]
        assert "HEAD" in err.message

    def test_staged_only_change_refused(self, repo: Path) -> None:
        (repo / "README.md").write_text("staged\n")
        _git(repo, "add", "README.md")

        err = _assert_refused_before_adapter(repo)

        assert err.detail["dirty_count"] == 1

    def test_untracked_stray_file_refused(self, repo: Path) -> None:
        (repo / "stray.txt").write_text("oops\n")

        err = _assert_refused_before_adapter(repo)

        assert "stray.txt" in err.detail["dirty_paths"]

    def test_message_lists_at_most_ten_paths(self, repo: Path) -> None:
        for i in range(12):
            (repo / f"stray-{i:02d}.txt").write_text("x\n")

        err = _assert_refused_before_adapter(repo)

        assert len(err.detail["dirty_paths"]) == 10
        assert err.detail["dirty_count"] == 12
        assert "(and 2 more)" in err.message


class TestCodeprobeOwnedArtifactsAllowed:
    def test_codeprobe_owned_untracked_files_do_not_refuse(
        self, repo: Path
    ) -> None:
        """Untracked entries under .codeprobe/, runs/, worktree dirs, and
        experiment dirs never trigger the refusal (the experiment fixture
        itself is untracked under .codeprobe/)."""
        (repo / "runs").mkdir()
        (repo / "runs" / "stray.txt").write_text("run artifact\n")
        (repo / ".codeprobe-worktrees-ab12").mkdir()
        (repo / ".codeprobe-worktrees-ab12" / "leftover.txt").write_text("x\n")
        myexp = repo / "myexp"
        myexp.mkdir()
        (myexp / "experiment.json").write_text("{}")
        (myexp / "junk.txt").write_text("x\n")

        adapter = FakeAdapter(cost_usd=0.0, cost_model="unknown", duration=0.0)
        with (
            patch.object(run_cmd_mod, "resolve", return_value=adapter),
            patch.object(
                run_cmd_mod, "execute_config", MagicMock(return_value=[])
            ) as mock_execute,
        ):
            run_cmd_mod.run_eval(
                str(repo / ".codeprobe" / "exp"), agent="fake", parallel=1,
                quiet=True, force_plain=True,
            )

        assert mock_execute.called


class TestAllowDirty:
    def test_allow_dirty_proceeds_with_disclosure(
        self, repo: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (repo / "README.md").write_text("modified\n")

        adapter = FakeAdapter(cost_usd=0.0, cost_model="unknown", duration=0.0)
        with (
            patch.object(run_cmd_mod, "resolve", return_value=adapter),
            patch.object(
                run_cmd_mod, "execute_config", MagicMock(return_value=[])
            ) as mock_execute,
        ):
            run_cmd_mod.run_eval(
                str(repo / ".codeprobe" / "exp"), agent="fake", parallel=1,
                quiet=True, force_plain=True, allow_dirty=True,
            )

        assert mock_execute.called
        stderr = capsys.readouterr().err
        assert "--allow-dirty" in stderr
        assert "will NOT be visible to agents" in stderr


class TestDryRun:
    def test_dry_run_on_dirty_repo_does_not_refuse(self, repo: Path) -> None:
        (repo / "README.md").write_text("modified\n")

        adapter = FakeAdapter(cost_usd=0.0, cost_model="unknown", duration=0.0)
        with patch.object(run_cmd_mod, "resolve", return_value=adapter):
            run_cmd_mod.run_eval(
                str(repo / ".codeprobe" / "exp"), agent="fake", parallel=1,
                quiet=True, force_plain=True, dry_run=True,
            )


class TestNotAGitRepo:
    def test_non_git_directory_raises_not_a_git_repo(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "plain"
        root.mkdir()
        _setup_experiment(root)

        with (
            patch.object(run_cmd_mod, "resolve") as mock_resolve,
            patch.object(run_cmd_mod, "execute_config") as mock_execute,
        ):
            with pytest.raises(DiagnosticError) as exc_info:
                run_cmd_mod.run_eval(
                    str(root / ".codeprobe" / "exp"), agent="fake",
                    parallel=1, quiet=True, force_plain=True,
                )

        assert exc_info.value.code == "NOT_A_GIT_REPO"
        assert not mock_resolve.called
        assert not mock_execute.called


class TestCliSurface:
    def test_cli_dirty_refusal_emits_json_envelope(self, repo: Path) -> None:
        from click.testing import CliRunner

        from codeprobe.cli import main

        (repo / "README.md").write_text("modified\n")

        with patch.object(run_cmd_mod, "execute_config") as mock_execute:
            result = CliRunner().invoke(
                main,
                [
                    "run",
                    str(repo / ".codeprobe" / "exp"),
                    "--agent", "fake",
                    "--tenant", "t-dirty",
                    "--json",
                ],
            )

        assert result.exit_code != 0
        assert not mock_execute.called
        envelope = None
        for line in result.output.splitlines():
            if line.startswith("{"):
                candidate = json.loads(line)
                if candidate.get("record_type") == "envelope":
                    envelope = candidate
        assert envelope is not None, result.output
        assert envelope["error"]["code"] == "DIRTY_CHECKOUT"
        assert envelope["error"]["next_try_flag"] == "--allow-dirty"

    def test_run_help_discloses_allow_dirty(self) -> None:
        from click.testing import CliRunner

        from codeprobe.cli import main

        result = CliRunner().invoke(main, ["run", "--help"])
        assert result.exit_code == 0, result.output
        assert "--allow-dirty" in result.output
        assert "DIRTY_CHECKOUT" in result.output
