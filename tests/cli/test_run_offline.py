"""Tests for ``codeprobe run --offline`` wiring.

Finding BC-C-02 (v0.6.0-batch-c review): ``run --offline`` must invoke
the ``check-infra offline`` credential-TTL preflight and fail-loud when
it rejects the environment, BEFORE any adapter is resolved or any task
is dispatched. On success it also sets ``CODEPROBE_OFFLINE=1`` so
subsystem callers can short-circuit network I/O.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.conftest import FakeAdapter


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _clear_creds(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "AWS_SESSION_EXPIRATION",
        "AWS_CREDENTIAL_EXPIRATION",
        "GOOGLE_APPLICATION_CREDENTIALS_TOKEN_EXPIRY",
        "AZURE_TOKEN_EXPIRES_ON",
        "CODEPROBE_OFFLINE",
    ):
        monkeypatch.delenv(var, raising=False)


def _init_git_repo(repo: Path) -> None:
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "r@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "r"],
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("seed\n")
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


def _setup_experiment(root: Path) -> tuple[Path, str]:
    exp_dir = root / ".codeprobe" / "exp"
    tasks_dir = exp_dir / "tasks"
    task_id = "task-001"
    task_dir = tasks_dir / task_id
    task_dir.mkdir(parents=True)

    (task_dir / "instruction.md").write_text("Do stuff.", encoding="utf-8")

    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    test_sh = tests_dir / "test.sh"
    test_sh.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
    test_sh.chmod(
        test_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    )

    experiment_json = {
        "name": "exp",
        "description": "offline test",
        "tasks_dir": "tasks",
        "task_ids": [task_id],
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
    return exp_dir, task_id


def test_run_offline_success_runs_eval_and_sets_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a healthy TTL matrix, --offline completes preflight and runs."""
    _clear_creds(monkeypatch)
    future = datetime.now(tz=UTC) + timedelta(hours=8)
    monkeypatch.setenv("AWS_SESSION_EXPIRATION", _iso(future))
    monkeypatch.setenv(
        "GOOGLE_APPLICATION_CREDENTIALS_TOKEN_EXPIRY", _iso(future)
    )
    monkeypatch.setenv("AZURE_TOKEN_EXPIRES_ON", _iso(future))

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    exp_dir, _ = _setup_experiment(repo)

    adapter = FakeAdapter(
        stdout="ok", cost_usd=0.0, cost_model="unknown", duration=0.0
    )

    from codeprobe.cli import run_cmd as run_cmd_mod

    assert os.environ.get("CODEPROBE_OFFLINE") is None

    with patch.object(run_cmd_mod, "resolve", return_value=adapter):
        run_cmd_mod.run_eval(
            str(exp_dir),
            agent="fake",
            parallel=1,
            quiet=True,
            force_plain=True,
            offline=True,
            offline_expected_run_duration="1h",
        )

    # The adapter was actually invoked (run proceeded past preflight).
    assert adapter.run_calls, "adapter.run must be called after successful preflight"
    # CODEPROBE_OFFLINE was exported for subprocesses.
    assert os.environ.get("CODEPROBE_OFFLINE") == "1"


def test_run_offline_failed_preflight_exits_before_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Preflight failure propagates and adapter is never invoked."""
    _clear_creds(monkeypatch)
    # Bedrock session expires in 5 minutes < 1h expected run duration.
    soon = datetime.now(tz=UTC) + timedelta(minutes=5)
    monkeypatch.setenv("AWS_SESSION_EXPIRATION", _iso(soon))

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    exp_dir, _ = _setup_experiment(repo)

    adapter = FakeAdapter(
        stdout="ok", cost_usd=0.0, cost_model="unknown", duration=0.0
    )

    import click

    from codeprobe.cli import run_cmd as run_cmd_mod

    with patch.object(run_cmd_mod, "resolve", return_value=adapter):
        with pytest.raises(click.ClickException) as excinfo:
            run_cmd_mod.run_eval(
                str(exp_dir),
                agent="fake",
                parallel=1,
                quiet=True,
                force_plain=True,
                offline=True,
                offline_expected_run_duration="1h",
            )

    # Adapter MUST NOT have been invoked — preflight failed early.
    assert not adapter.run_calls, (
        "adapter.run must not be called when --offline preflight fails"
    )
    # The error message names the failing backend.
    assert "bedrock" in str(excinfo.value.message).lower()
    # CODEPROBE_OFFLINE must NOT be set when preflight fails.
    assert os.environ.get("CODEPROBE_OFFLINE") is None


def test_run_without_offline_does_not_run_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Baseline: without --offline, preflight is not invoked."""
    _clear_creds(monkeypatch)
    # Set an EXPIRED Bedrock token — would fail preflight if invoked.
    past = datetime.now(tz=UTC) - timedelta(hours=1)
    monkeypatch.setenv("AWS_SESSION_EXPIRATION", _iso(past))

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)

    exp_dir, _ = _setup_experiment(repo)

    adapter = FakeAdapter(
        stdout="ok", cost_usd=0.0, cost_model="unknown", duration=0.0
    )

    from codeprobe.cli import run_cmd as run_cmd_mod

    with patch.object(run_cmd_mod, "resolve", return_value=adapter):
        # No offline flag; expired creds do not block.
        run_cmd_mod.run_eval(
            str(exp_dir),
            agent="fake",
            parallel=1,
            quiet=True,
            force_plain=True,
        )

    assert adapter.run_calls, "adapter.run must be called in normal mode"


def test_run_cli_accepts_offline_flag() -> None:
    """The Click ``--offline`` option exists on the ``run`` command.

    Regression test for BC-C-02: before the fix ``codeprobe run --offline``
    produced ``UsageError: no such option``.
    """
    from click.testing import CliRunner

    from codeprobe.cli import main

    result = CliRunner().invoke(main, ["run", "--help"])
    assert result.exit_code == 0, result.output
    assert "--offline" in result.output
    assert "--offline-expected-run-duration" in result.output
