"""Integration test: SIGINT → graceful shutdown with exit code 130, no traceback."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@test.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@test.com",
}


def _make_experiment_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with a valid codeprobe experiment layout.

    The experiment has two tasks whose test.sh scripts sleep for 30s, ensuring
    the process is alive long enough to receive SIGINT.
    """
    repo = tmp_path / "exp"
    repo.mkdir()

    # experiment.json --------------------------------------------------------
    experiment = {
        "name": "ctrlc-test",
        "description": "Signal handling integration test",
        "configs": [{"label": "baseline", "agent": "claude"}],
    }
    (repo / "experiment.json").write_text(json.dumps(experiment, indent=2))

    # tasks ------------------------------------------------------------------
    for task_name in ("task-a", "task-b"):
        task_dir = repo / "tasks" / task_name
        tests_dir = task_dir / "tests"
        tests_dir.mkdir(parents=True)
        (task_dir / "instruction.md").write_text("Do something.\n")
        test_sh = tests_dir / "test.sh"
        test_sh.write_text("#!/usr/bin/env bash\nsleep 30 && exit 0\n")
        test_sh.chmod(0o755)

    # git init (codeprobe run requires a git repo) --------------------------
    env = {**os.environ, **_GIT_ENV, "HOME": str(tmp_path)}
    subprocess.run(
        ["git", "init"], cwd=str(repo), capture_output=True, check=True, env=env
    )
    subprocess.run(
        ["git", "add", "."], cwd=str(repo), capture_output=True, check=True, env=env
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(repo),
        capture_output=True,
        check=True,
        env=env,
    )
    return repo


@pytest.mark.integration
@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT not portable on Windows")
def test_sigint_produces_exit_130_no_traceback(tmp_path: Path) -> None:
    """Sending SIGINT to ``codeprobe run`` must exit 130 without a traceback."""
    repo = _make_experiment_repo(tmp_path)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "codeprobe",
            "run",
            ".",
            "--parallel",
            "1",
            "--force-plain",
            "--agent",
            "claude",
        ],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Start in a new process group so SIGINT only hits the child tree.
        preexec_fn=os.setsid,
    )

    # Give the process time to start up and enter the run loop.
    time.sleep(3)

    # Send SIGINT to the entire process group.
    os.killpg(os.getpgid(proc.pid), signal.SIGINT)

    try:
        _stdout, stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail("Process did not exit within 10s after SIGINT")

    stderr_text = stderr.decode("utf-8", errors="replace")

    # The handler raises SystemExit(130).
    assert (
        proc.returncode == 130
    ), f"Expected exit code 130, got {proc.returncode}.\nstderr:\n{stderr_text}"

    # A clean shutdown must not dump a Python traceback.
    assert (
        "Traceback (most recent call last)" not in stderr_text
    ), f"Unexpected traceback in stderr:\n{stderr_text}"
