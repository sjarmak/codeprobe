"""Shared pytest fixtures and helpers for the capability test matrix."""

from __future__ import annotations

import json
import stat
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from tests.capabilities.fixtures import OracleFixture

# ---------------------------------------------------------------------------
# Marker registration — per pyproject.toml [tool.pytest.ini_options] only the
# "integration" marker is declared. Capability tests use "capability" and
# "matrix" markers to let users select/exclude them separately.
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "capability: end-to-end capability regression matrix (tests/capabilities/)",
    )
    config.addinivalue_line(
        "markers",
        "matrix: part of the cross-corpus / cross-language matrix",
    )


# ---------------------------------------------------------------------------
# Fixture availability guard
# ---------------------------------------------------------------------------


@pytest.fixture()
def require_oracle() -> Callable[[OracleFixture], None]:
    """Return a helper that skips the calling test when the oracle is absent.

    Keeping the skip reason structured (capability, corpus, path) satisfies
    the "actionable failure output" criterion — a CI run missing an oracle
    clearly states which corpus and path are expected.
    """

    def _require(fixture: OracleFixture) -> None:
        if not fixture.exists():
            pytest.skip(fixture.skip_reason())

    return _require


# ---------------------------------------------------------------------------
# CliRunner — in-process Click invocation (AC: real CLI, not mocks)
# ---------------------------------------------------------------------------


@pytest.fixture()
def cli_runner() -> CliRunner:
    """Provide a Click CliRunner.

    Click 8.3 removed ``mix_stderr``; stderr and stdout are now always
    captured separately on ``result.stderr`` / ``result.output``.
    """
    return CliRunner()


# ---------------------------------------------------------------------------
# Minimal repo scaffolding — used by capabilities that require a git repo
# (assess, mine, probe) or a codeprobe task layout (validate, run).
# ---------------------------------------------------------------------------


def _run_git(args: list[str], cwd: Path) -> None:
    """Run git with captured output; fail the test if git itself errors."""
    subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


@pytest.fixture()
def minimal_git_repo(tmp_path: Path) -> Path:
    """Create a tiny git repo with a single commit so worktree / assess work."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(["init", "-q", "-b", "main"], repo)
    _run_git(["config", "user.email", "capability-test@example.com"], repo)
    _run_git(["config", "user.name", "Capability Test"], repo)
    (repo / "README.md").write_text("capability test seed\n", encoding="utf-8")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text(
        "def main():\n    return 0\n", encoding="utf-8"
    )
    _run_git(["add", "README.md", "src/main.py"], repo)
    _run_git(["commit", "-q", "-m", "seed"], repo)
    return repo


# ---------------------------------------------------------------------------
# Task-directory builders — mirror what codeprobe.writer produces.
# ---------------------------------------------------------------------------


def _chmod_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_test_script(tests_dir: Path, *, exit_code: int = 0) -> Path:
    tests_dir.mkdir(parents=True, exist_ok=True)
    script = tests_dir / "test.sh"
    script.write_text(f"#!/bin/bash\nexit {exit_code}\n", encoding="utf-8")
    _chmod_executable(script)
    return script


@pytest.fixture()
def make_task_dir(tmp_path: Path) -> Callable[..., Path]:
    """Factory for minimal task directories.

    Returns a callable(name, *, task_type, verification_mode, language,
    passing, ground_truth) that produces a directory compatible with
    ``codeprobe validate`` and ``codeprobe run``.
    """

    def _build(
        name: str,
        *,
        task_type: str = "sdlc_code_change",
        verification_mode: str = "test_script",
        language: str = "python",
        passing: bool = True,
        ground_truth: dict | None = None,
    ) -> Path:
        task_dir = tmp_path / name
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "instruction.md").write_text(
            f"# {name}\n\nLanguage: {language}.\n\nApply the change.\n",
            encoding="utf-8",
        )
        (task_dir / "task.toml").write_text(
            f'[metadata]\n'
            f'name = "{name}"\n'
            f'task_type = "{task_type}"\n'
            f'language = "{language}"\n\n'
            f'[verification]\n'
            f'verification_mode = "{verification_mode}"\n',
            encoding="utf-8",
        )
        _write_test_script(task_dir / "tests", exit_code=0 if passing else 1)
        if ground_truth is not None:
            (task_dir / "tests" / "ground_truth.json").write_text(
                json.dumps(ground_truth, indent=2) + "\n", encoding="utf-8"
            )
        return task_dir

    return _build


# ---------------------------------------------------------------------------
# Schema-shape assertions — structural, not string-exact (per AC).
# ---------------------------------------------------------------------------


def assert_has_fields(obj: object, required: list[str], *, context: str) -> None:
    """Raise AssertionError naming the missing field(s) and context."""
    assert isinstance(obj, dict), f"{context}: expected dict, got {type(obj).__name__}"
    missing = [f for f in required if f not in obj]
    assert not missing, f"{context}: missing required fields {missing}"


def assert_non_empty_string(value: object, *, context: str) -> None:
    assert isinstance(value, str), f"{context}: expected str, got {type(value).__name__}"
    assert value.strip(), f"{context}: expected non-empty string"


__all__ = [
    "_run_git",
    "assert_has_fields",
    "assert_non_empty_string",
    "cli_runner",
    "make_task_dir",
    "minimal_git_repo",
    "require_oracle",
]
