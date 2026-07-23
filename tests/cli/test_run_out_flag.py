"""Tests for the ``--out`` flag on ``codeprobe run`` (codeprobe-xcue).

BUG-OUT-FLAG-002 requires ``mine``/``run``/``interpret`` to all accept
``--out`` for custom output paths. This module covers ``run``: the
``--help`` surface check plus functional redirection of results
(``runs/<config>/results.json``) to a custom directory, and confirms
omitting the flag preserves the pre-existing default write location.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from tests.conftest import FakeAdapter


def _init_git_repo(repo: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    for cfg in (["user.email", "r@example.com"], ["user.name", "r"]):
        subprocess.run(
            ["git", "-C", str(repo), "config", *cfg],
            check=True,
            capture_output=True,
        )
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "seed"],
        check=True,
        capture_output=True,
    )


def _setup_experiment(repo: Path) -> Path:
    """Create a minimal one-task experiment under <repo>/.codeprobe/exp."""
    exp_dir = repo / ".codeprobe" / "exp"
    task_dir = exp_dir / "tasks" / "task-001"
    tests_dir = task_dir / "tests"
    tests_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("Do stuff.\n", encoding="utf-8")
    test_sh = tests_dir / "test.sh"
    test_sh.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    test_sh.chmod(0o755)
    experiment = {
        "name": "exp",
        "description": "--out redirection test",
        "tasks_dir": "tasks",
        "task_ids": ["task-001"],
        "configs": [
            {
                "label": "baseline",
                "agent": "fake",
                "model": None,
                "extra": {"timeout_seconds": 60},
            },
        ],
    }
    (exp_dir / "experiment.json").write_text(
        json.dumps(experiment), encoding="utf-8"
    )
    return exp_dir


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_git_repo(repo_dir)
    _setup_experiment(repo_dir)
    return repo_dir


def test_run_help_contains_out() -> None:
    """BUG-OUT-FLAG-002: `codeprobe run --help` must advertise --out."""
    result = CliRunner().invoke(main, ["run", "--help"])
    assert result.exit_code == 0, result.output
    assert "--out" in result.output


def test_run_without_out_writes_results_to_experiment_dir(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting --out preserves the pre-existing default write location."""
    adapter = FakeAdapter(cost_usd=0.0, cost_model="unknown", duration=0.0)
    monkeypatch.setattr("codeprobe.cli.run_cmd.resolve", lambda _name: adapter)

    exp_dir = repo / ".codeprobe" / "exp"
    result = CliRunner().invoke(
        main,
        [
            "run",
            str(exp_dir),
            "--agent",
            "fake",
            "--parallel",
            "1",
            "--force-plain",
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    default_results = exp_dir / "runs" / "baseline" / "results.json"
    assert default_results.is_file()


def test_run_out_redirects_results(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--out redirects runs/<config>/results.json off the experiment dir."""
    adapter = FakeAdapter(cost_usd=0.0, cost_model="unknown", duration=0.0)
    monkeypatch.setattr("codeprobe.cli.run_cmd.resolve", lambda _name: adapter)

    exp_dir = repo / ".codeprobe" / "exp"
    out_dir = tmp_path / "custom-results"
    out_dir.mkdir()

    result = CliRunner().invoke(
        main,
        [
            "run",
            str(exp_dir),
            "--agent",
            "fake",
            "--parallel",
            "1",
            "--force-plain",
            "--out",
            str(out_dir),
        ],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output

    default_results = exp_dir / "runs" / "baseline" / "results.json"
    custom_results = out_dir / "runs" / "baseline" / "results.json"
    assert not default_results.exists(), (
        "results must NOT land at the default location when --out is passed"
    )
    assert custom_results.is_file()

    data = json.loads(custom_results.read_text())
    assert data["config"] == "baseline"

    # experiment.json itself is untouched by --out.
    assert (exp_dir / "experiment.json").is_file()


def test_run_out_rejects_missing_parent_directory(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = FakeAdapter(cost_usd=0.0, cost_model="unknown", duration=0.0)
    monkeypatch.setattr("codeprobe.cli.run_cmd.resolve", lambda _name: adapter)

    exp_dir = repo / ".codeprobe" / "exp"
    bad_out = tmp_path / "does-not-exist" / "out"

    result = CliRunner().invoke(
        main,
        [
            "run",
            str(exp_dir),
            "--agent",
            "fake",
            "--parallel",
            "1",
            "--force-plain",
            "--out",
            str(bad_out),
        ],
    )
    assert result.exit_code != 0
    assert "does not exist" in result.output
