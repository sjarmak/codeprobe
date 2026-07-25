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

    The experiment has two valid tasks; the fake agent process blocks until
    the test delivers SIGINT.
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
        test_sh.write_text("#!/usr/bin/env bash\nexit 0\n")
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


def _make_blocking_claude(tmp_path: Path) -> tuple[Path, Path]:
    """Create a deterministic Claude stand-in that exits quietly on signals."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "claude"
    ready_file = tmp_path / "claude-ready"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import signal\n"
        "import time\n"
        "from pathlib import Path\n"
        "\n"
        "def stop(*_args: object) -> None:\n"
        "    raise SystemExit(130)\n"
        "\n"
        "signal.signal(signal.SIGINT, stop)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"Path({str(ready_file)!r}).touch()\n"
        "while True:\n"
        "    time.sleep(30)\n"
    )
    binary.chmod(0o755)
    return bin_dir, ready_file


def _wait_for_ready_file(
    proc: subprocess.Popen[bytes],
    ready_file: Path,
    *,
    timeout: float,
) -> None:
    """Wait until the fake agent has installed its signal handlers."""
    deadline = time.monotonic() + timeout
    while not ready_file.exists():
        if proc.poll() is not None:
            pytest.fail(
                f"Process exited with {proc.returncode} before agent readiness"
            )
        if time.monotonic() >= deadline:
            pytest.fail(f"Agent was not ready within {timeout}s")
        time.sleep(0.01)


@pytest.mark.integration
@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT not portable on Windows")
def test_sigint_produces_exit_130_no_traceback(tmp_path: Path) -> None:
    """Sending SIGINT to ``codeprobe run`` must exit 130 without a traceback."""
    repo = _make_experiment_repo(tmp_path)
    bin_dir, ready_file = _make_blocking_claude(tmp_path)
    test_home = tmp_path / "home"
    claude_config = tmp_path / "claude-config"
    test_home.mkdir()
    claude_config.mkdir()
    auth_variables = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
    }
    inherited_env = {
        key: value
        for key, value in os.environ.items()
        if key not in auth_variables
    }
    env = {
        **inherited_env,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(test_home),
        "CLAUDE_CONFIG_DIR": str(claude_config),
    }

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
            "--uncontained",
        ],
        cwd=str(repo),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        # Start in a new process group so SIGINT only hits the child tree.
        start_new_session=True,
    )

    stderr = b""
    try:
        _wait_for_ready_file(proc, ready_file, timeout=10)
        os.killpg(proc.pid, signal.SIGINT)
        _stdout, stderr = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        pytest.fail("Process did not exit within 10s after SIGINT")
    finally:
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.communicate()

    stderr_text = stderr.decode("utf-8", errors="replace")

    # The handler raises SystemExit(130).
    assert (
        proc.returncode == 130
    ), f"Expected exit code 130, got {proc.returncode}.\nstderr:\n{stderr_text}"

    # A clean shutdown must not dump a Python traceback.
    assert (
        "Traceback (most recent call last)" not in stderr_text
    ), f"Unexpected traceback in stderr:\n{stderr_text}"
