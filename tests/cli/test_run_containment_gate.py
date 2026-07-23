"""Tests for the containment gate on ``codeprobe run`` (codeprobe-f7rl.3).

A real run launches an autonomous agent with
``--dangerously-skip-permissions`` plus mined third-party test/verifier
scripts. Outside a container (and without the user-set
``CODEPROBE_SANDBOX=1`` consent signal) ``codeprobe run`` must hard-refuse
with UNCONTAINED_REFUSED unless ``--uncontained`` is passed, and codeprobe
must never satisfy its own gate by writing CODEPROBE_SANDBOX.

The suite-wide ``_containment_consent_env`` fixture (tests/conftest.py)
sets CODEPROBE_SANDBOX=1; tests here delete it and pin
``codeprobe.core.sandbox.is_sandboxed`` so both branches are exercised
deterministically on any host.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codeprobe.cli import run_cmd as run_cmd_mod
from codeprobe.cli.errors import PrescriptiveError
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
        "description": "containment gate test",
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


@pytest.fixture
def bare_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Simulate an uncontained host: no container, no user consent signal."""
    monkeypatch.delenv("CODEPROBE_SANDBOX", raising=False)
    monkeypatch.setattr("codeprobe.core.sandbox.is_sandboxed", lambda: False)


class TestUncontainedRefusal:
    def test_bare_host_refuses_before_config_dispatch(
        self, repo: Path, bare_host: None
    ) -> None:
        adapter = FakeAdapter(cost_usd=0.0, cost_model="unknown", duration=0.0)
        with (
            patch.object(run_cmd_mod, "resolve", return_value=adapter),
            patch.object(run_cmd_mod, "execute_config") as mock_execute,
        ):
            with pytest.raises(PrescriptiveError) as exc_info:
                run_cmd_mod.run_eval(
                    str(repo / ".codeprobe" / "exp"), agent="fake",
                    parallel=1, quiet=True, force_plain=True,
                )

        assert not mock_execute.called, "execute_config must not be reached"
        err = exc_info.value
        assert err.code == "UNCONTAINED_REFUSED"
        assert err.next_try_flag == "--uncontained"
        assert "--dangerously-skip-permissions" in err.message
        assert "filesystem, credential, and network access" in err.message

    def test_dry_run_is_exempt(self, repo: Path, bare_host: None) -> None:
        adapter = FakeAdapter(cost_usd=0.0, cost_model="unknown", duration=0.0)
        with patch.object(run_cmd_mod, "resolve", return_value=adapter):
            run_cmd_mod.run_eval(
                str(repo / ".codeprobe" / "exp"), agent="fake", parallel=1,
                quiet=True, force_plain=True, dry_run=True,
            )


class TestUncontainedConsent:
    def test_uncontained_proceeds_with_disclosure_on_stderr(
        self,
        repo: Path,
        bare_host: None,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        adapter = FakeAdapter(cost_usd=0.0, cost_model="unknown", duration=0.0)
        with (
            patch.object(run_cmd_mod, "resolve", return_value=adapter),
            patch.object(
                run_cmd_mod, "execute_config", MagicMock(return_value=[])
            ) as mock_execute,
        ):
            run_cmd_mod.run_eval(
                str(repo / ".codeprobe" / "exp"), agent="fake", parallel=1,
                quiet=True, force_plain=True, uncontained=True,
            )

        assert mock_execute.called
        stderr = capsys.readouterr().err
        assert "--uncontained" in stderr
        assert "--dangerously-skip-permissions" in stderr
        assert "filesystem, credential, and network access" in stderr

    def test_run_never_sets_codeprobe_sandbox(
        self, repo: Path, bare_host: None
    ) -> None:
        """Regression: codeprobe must never satisfy its own gate."""
        adapter = FakeAdapter(cost_usd=0.0, cost_model="unknown", duration=0.0)
        with (
            patch.object(run_cmd_mod, "resolve", return_value=adapter),
            patch.object(
                run_cmd_mod, "execute_config", MagicMock(return_value=[])
            ),
        ):
            run_cmd_mod.run_eval(
                str(repo / ".codeprobe" / "exp"), agent="fake", parallel=1,
                quiet=True, force_plain=True, uncontained=True,
            )

        assert "CODEPROBE_SANDBOX" not in os.environ


class TestSandboxedHost:
    def test_sandboxed_host_proceeds_without_flag_or_disclosure(
        self,
        repo: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("CODEPROBE_SANDBOX", raising=False)
        monkeypatch.setattr(
            "codeprobe.core.sandbox.is_sandboxed", lambda: True
        )

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
        stderr = capsys.readouterr().err
        assert "--dangerously-skip-permissions" not in stderr
        assert "CODEPROBE_SANDBOX" not in os.environ


class TestCliSurface:
    def test_cli_refusal_emits_json_envelope(
        self, repo: Path, bare_host: None
    ) -> None:
        from click.testing import CliRunner

        from codeprobe.cli import main

        adapter = FakeAdapter(cost_usd=0.0, cost_model="unknown", duration=0.0)
        with (
            patch.object(run_cmd_mod, "resolve", return_value=adapter),
            patch.object(run_cmd_mod, "execute_config") as mock_execute,
        ):
            result = CliRunner().invoke(
                main,
                [
                    "run",
                    str(repo / ".codeprobe" / "exp"),
                    "--agent", "fake",
                    "--tenant", "t-containment",
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
        assert envelope["error"]["code"] == "UNCONTAINED_REFUSED"
        assert envelope["error"]["next_try_flag"] == "--uncontained"

    def test_run_help_discloses_containment(self) -> None:
        from click.testing import CliRunner

        from codeprobe.cli import main

        result = CliRunner().invoke(main, ["run", "--help"])
        assert result.exit_code == 0, result.output
        assert "--uncontained" in result.output
        assert "UNCONTAINED_REFUSED" in result.output
        assert "--dangerously-skip-permissions" in result.output
