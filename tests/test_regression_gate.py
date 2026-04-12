"""Tests for :mod:`acceptance.regression`.

These tests build minimal real git repos under ``tmp_path`` and drive the
regression gate against them with tiny fake ``python``, ``pytest``, ``ruff``,
and ``mypy`` shims on ``PATH``. Each shim either exits 0 or exits non-zero
based on a sentinel file the test writes before invocation — that gives the
gate a deterministic, fast proxy for "real" tool behavior without depending
on the actual test suite.

The fixtures intentionally never touch the codeprobe tree: they build
throwaway repos so running these tests neither requires nor mutates the
production source.
"""

from __future__ import annotations

import os
import stat
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from acceptance.regression import (
    RegressionResult,
    _maybe_revert,
    _run_check,
    main,
    run_regression_gate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command inside ``repo`` with deterministic identity env."""
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        }
    )
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    """Initialise a minimal git repo with a baseline commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("baseline\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _write_shim(
    bin_dir: Path,
    name: str,
    *,
    exit_code: int,
    message: str = "",
) -> None:
    """Write an executable shim that exits with ``exit_code``.

    A conditional form: if ``{bin_dir}/{name}.fail`` exists, the shim reads
    it for an exit code and message; otherwise uses the defaults.
    """
    shim = bin_dir / name
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'marker="{bin_dir}/{name}.fail"\n'
        'if [ -f "$marker" ]; then\n'
        '  cat "$marker"\n'
        "  exit 1\n"
        "fi\n"
        f'echo "{message}"\n'
        f"exit {exit_code}\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _write_python_shim(bin_dir: Path) -> None:
    """Write a ``python`` shim that routes ``python -m <tool>`` to the tool.

    The regression gate invokes ``python -m pytest`` / ``python -m ruff`` /
    ``python -m mypy``. Our shim pops the ``-m`` form and ``exec``s the
    corresponding tool shim from the same directory, so tests only need to
    configure per-tool success/failure.
    """
    shim = bin_dir / "python"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'bin_dir="{bin_dir}"\n'
        'if [ "$1" = "-m" ] && [ -n "$2" ]; then\n'
        '  tool="$2"\n'
        "  shift 2\n"
        '  exec "$bin_dir/$tool" "$@"\n'
        "fi\n"
        'echo "unexpected python invocation: $*" >&2\n'
        "exit 2\n"
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def gate_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """Build a repo + PATH of shims and return the pieces the tests need."""
    repo = _make_repo(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    _write_shim(bin_dir, "pytest", exit_code=0, message="pytest ok")
    _write_shim(bin_dir, "ruff", exit_code=0, message="ruff ok")
    _write_shim(bin_dir, "mypy", exit_code=0, message="mypy ok")
    _write_python_shim(bin_dir)

    # Shadow real tools by prepending the shim dir to PATH. git must still
    # resolve from the system PATH, so we append it after.
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")
    return {"repo": repo, "bin": bin_dir}


def _stage_fix_commit(repo: Path, payload: str) -> str:
    """Commit a trivial change so the gate has HEAD to revert."""
    (repo / "fix.txt").write_text(payload)
    _git(repo, "add", "fix.txt")
    _git(repo, "commit", "-q", "-m", "fix: candidate — test")
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


# ---------------------------------------------------------------------------
# RegressionResult dataclass
# ---------------------------------------------------------------------------


def test_regression_result_is_frozen() -> None:
    """RegressionResult must be immutable — required by ZFC / coding-style."""
    r = RegressionResult(passed=True, failed_check=None, reverted=False, output="")
    with pytest.raises(FrozenInstanceError):
        r.passed = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# run_regression_gate — happy path
# ---------------------------------------------------------------------------


def test_passing_commit_is_not_reverted(gate_env: dict[str, Path]) -> None:
    repo = gate_env["repo"]
    fix_sha = _stage_fix_commit(repo, "good change")

    result = run_regression_gate(
        repo,
        pytest_args=(),
        ruff_args=(),
        mypy_args=(),
    )

    assert result.passed is True
    assert result.failed_check is None
    assert result.reverted is False
    assert result.output == ""
    # HEAD must still be the Fix Agent's commit.
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert head == fix_sha


# ---------------------------------------------------------------------------
# run_regression_gate — failure paths
# ---------------------------------------------------------------------------


def test_pytest_failing_commit_is_reverted(gate_env: dict[str, Path]) -> None:
    repo = gate_env["repo"]
    bin_dir = gate_env["bin"]
    fix_sha = _stage_fix_commit(repo, "pytest-breaking")

    # Arm pytest shim to fail with a distinctive error message.
    (bin_dir / "pytest.fail").write_text("E   AssertionError: behavior drift\n")

    result = run_regression_gate(
        repo,
        pytest_args=(),
        ruff_args=(),
        mypy_args=(),
    )

    assert result.passed is False
    assert result.failed_check == "pytest"
    assert result.reverted is True
    assert "behavior drift" in result.output
    # git revert added a NEW commit on top; HEAD is no longer the fix SHA but
    # the fix commit is still reachable in history.
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert head != fix_sha
    log = _git(repo, "log", "--oneline", "-n", "3").stdout
    assert "Revert" in log
    # The working tree no longer contains the file the fix added.
    assert not (repo / "fix.txt").exists()


def test_ruff_failing_commit_is_reverted(gate_env: dict[str, Path]) -> None:
    repo = gate_env["repo"]
    bin_dir = gate_env["bin"]
    _stage_fix_commit(repo, "ruff-breaking")

    (bin_dir / "ruff.fail").write_text("E501 Line too long\n")

    result = run_regression_gate(
        repo,
        pytest_args=(),
        ruff_args=(),
        mypy_args=(),
    )

    assert result.passed is False
    assert result.failed_check == "ruff"
    assert result.reverted is True
    assert "Line too long" in result.output
    assert not (repo / "fix.txt").exists()


def test_mypy_failing_commit_is_reverted(gate_env: dict[str, Path]) -> None:
    repo = gate_env["repo"]
    bin_dir = gate_env["bin"]
    _stage_fix_commit(repo, "mypy-breaking")

    (bin_dir / "mypy.fail").write_text("error: Incompatible types [arg-type]\n")

    result = run_regression_gate(
        repo,
        pytest_args=(),
        ruff_args=(),
        mypy_args=(),
    )

    assert result.passed is False
    assert result.failed_check == "mypy"
    assert result.reverted is True
    assert "Incompatible types" in result.output
    assert not (repo / "fix.txt").exists()


def test_failure_output_is_captured(gate_env: dict[str, Path]) -> None:
    """The captured output must actually land in the result.output field."""
    repo = gate_env["repo"]
    bin_dir = gate_env["bin"]
    _stage_fix_commit(repo, "captured-output")

    distinctive = "DISTINCTIVE-TOKEN-2f3a"
    (bin_dir / "pytest.fail").write_text(f"{distinctive}\n")

    result = run_regression_gate(
        repo,
        pytest_args=(),
        ruff_args=(),
        mypy_args=(),
    )
    assert distinctive in result.output


# ---------------------------------------------------------------------------
# run_regression_gate — dry-run mode
# ---------------------------------------------------------------------------


def test_revert_on_failure_false_preserves_commit(
    gate_env: dict[str, Path],
) -> None:
    """With revert_on_failure=False, the gate reports failure but keeps HEAD."""
    repo = gate_env["repo"]
    bin_dir = gate_env["bin"]
    fix_sha = _stage_fix_commit(repo, "dry-run")

    (bin_dir / "pytest.fail").write_text("pretend-failure\n")

    result = run_regression_gate(
        repo,
        revert_on_failure=False,
        pytest_args=(),
        ruff_args=(),
        mypy_args=(),
    )

    assert result.passed is False
    assert result.failed_check == "pytest"
    assert result.reverted is False
    # HEAD is unchanged — the Fix Agent's commit stayed in place.
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert head == fix_sha
    assert (repo / "fix.txt").exists()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_missing_repo_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        run_regression_gate(tmp_path / "does-not-exist")


def test_non_git_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        run_regression_gate(tmp_path)


# ---------------------------------------------------------------------------
# _run_check internals
# ---------------------------------------------------------------------------


def test_run_check_returns_output_on_failure(
    gate_env: dict[str, Path],
) -> None:
    repo = gate_env["repo"]
    bin_dir = gate_env["bin"]
    (bin_dir / "pytest.fail").write_text("captured-via-run-check\n")
    rc, output = _run_check(
        ("python", "-m", "pytest"),
        cwd=repo,
        timeout_s=30.0,
    )
    assert rc != 0
    assert "captured-via-run-check" in output


def test_run_check_missing_executable(tmp_path: Path) -> None:
    rc, output = _run_check(
        ("definitely-not-a-real-binary-xyz",),
        cwd=tmp_path,
        timeout_s=5.0,
    )
    assert rc != 0
    assert "command not found" in output or "not-a-real-binary" in output


# ---------------------------------------------------------------------------
# _maybe_revert internals
# ---------------------------------------------------------------------------


def test_maybe_revert_disabled(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    reverted, output = _maybe_revert(repo, enabled=False)
    assert reverted is False
    assert output == ""


def test_maybe_revert_enabled_reverts_head(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _stage_fix_commit(repo, "revert-me")
    pre_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    reverted, _ = _maybe_revert(repo, enabled=True)
    assert reverted is True
    post_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert post_head != pre_head
    log = _git(repo, "log", "--oneline", "-n", "2").stdout
    assert "Revert" in log


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def test_cli_main_success(
    gate_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = gate_env["repo"]
    _stage_fix_commit(repo, "cli-success")

    # Monkeypatch run_regression_gate defaults via argv — main() uses the
    # production defaults otherwise, which reference tests/ and src/codeprobe
    # that do not exist in our throwaway repo. Instead we call
    # run_regression_gate directly: this test covers main() with a repo that
    # the shims will say is fine regardless of arguments.
    from acceptance import regression as reg

    def fake_gate(*args, **kwargs):  # type: ignore[no-untyped-def]
        return RegressionResult(
            passed=True, failed_check=None, reverted=False, output=""
        )

    monkeypatch.setattr(reg, "run_regression_gate", fake_gate)
    exit_code = main(["--repo-root", str(repo)])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "REGRESSION-GATE PASSED" in captured.out


def test_cli_main_failure(
    gate_env: dict[str, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = gate_env["repo"]
    _stage_fix_commit(repo, "cli-failure")

    from acceptance import regression as reg

    def fake_gate(*args, **kwargs):  # type: ignore[no-untyped-def]
        return RegressionResult(
            passed=False,
            failed_check="pytest",
            reverted=True,
            output="fake failure captured",
        )

    monkeypatch.setattr(reg, "run_regression_gate", fake_gate)
    exit_code = main(["--repo-root", str(repo)])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert "REGRESSION-GATE FAILED" in captured.out
    assert "check=pytest" in captured.out
    assert "reverted=True" in captured.out
    assert "fake failure captured" in captured.out


def test_cli_main_invalid_repo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["--repo-root", str(tmp_path / "not-a-repo")])
    captured = capsys.readouterr()
    assert exit_code == 2
    assert "ERROR" in captured.err
