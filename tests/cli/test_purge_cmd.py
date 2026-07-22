"""Tests for ``codeprobe purge`` — cleartext run-artifact retention lever.

CliRunner pattern per ``tests/cli/test_cache_cmd.py``. The autouse
``fake_tempdir`` fixture points ``tempfile.gettempdir()`` at a per-test
directory so no test can ever sweep the real system temp directory.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli.errors import DiagnosticError
from codeprobe.cli.purge_cmd import purge

_DAY = 86_400.0

_README_PATH = Path(__file__).resolve().parents[2] / "README.md"


@pytest.fixture(autouse=True)
def fake_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    fake = tmp_path / "faketmp"
    fake.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(fake))
    return fake


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _seed_experiment(repo: Path, name: str) -> Path:
    """Create ``.codeprobe/<name>/`` with run artifacts; return the exp dir."""
    exp = repo / ".codeprobe" / name
    (exp / "runs" / "base" / "t1").mkdir(parents=True)
    (exp / "runs" / "base" / "t1" / "agent_output.txt").write_text(
        "transcript quoting customer source\n"
    )
    (exp / "runs" / "trace.db").write_text("trace-bytes")
    (exp / "experiment.json").write_text("{}")
    (exp / "tasks").mkdir()
    return exp


def _seed_repo(tmp_path: Path) -> Path:
    """Customer checkout with one experiment plus untouched source."""
    repo = tmp_path / "repo"
    _seed_experiment(repo, "exp1")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('customer source')\n")
    return repo


def _age_tree(root: Path, days: float) -> None:
    """Set mtime of *root* and everything under it to *days* days ago."""
    stamp = time.time() - days * _DAY
    for entry in (root, *root.rglob("*")):
        os.utime(entry, (stamp, stamp))


class TestPurgeDryRun:
    def test_dry_run_lists_and_deletes_nothing(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        repo = _seed_repo(tmp_path)
        runs = repo / ".codeprobe" / "exp1" / "runs"

        result = runner.invoke(purge, [str(repo)])

        assert result.exit_code == 0, result.output
        assert str(runs) in result.output
        assert "cleartext" in result.output.lower()
        assert (runs / "base" / "t1" / "agent_output.txt").exists()
        assert (runs / "trace.db").exists()
        assert (repo / "src" / "app.py").exists()

    def test_no_candidates_is_clean_noop(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        repo = tmp_path / "empty-repo"
        repo.mkdir()

        result = runner.invoke(purge, [str(repo)])

        assert result.exit_code == 0, result.output
        assert "nothing to purge" in result.output.lower()


class TestPurgeDelete:
    def test_yes_deletes_runs_but_not_source_or_experiment_json(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        repo = _seed_repo(tmp_path)
        exp = repo / ".codeprobe" / "exp1"

        result = runner.invoke(purge, [str(repo), "--yes"])

        assert result.exit_code == 0, result.output
        assert not (exp / "runs").exists()
        assert (repo / "src" / "app.py").exists()
        assert (exp / "experiment.json").exists()
        assert (exp / "tasks").exists()

    def test_older_than_filters_by_mtime(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        repo = _seed_repo(tmp_path)
        old_runs = repo / ".codeprobe" / "exp1" / "runs"
        fresh_runs = _seed_experiment(repo, "exp2") / "runs"
        _age_tree(old_runs, days=40)

        result = runner.invoke(purge, [str(repo), "--older-than", "30", "--yes"])

        assert result.exit_code == 0, result.output
        assert not old_runs.exists()
        assert fresh_runs.exists()
        assert (fresh_runs / "trace.db").exists()

    def test_all_removes_experiment_dirs(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        repo = _seed_repo(tmp_path)

        result = runner.invoke(purge, [str(repo), "--all", "--yes"])

        assert result.exit_code == 0, result.output
        assert not (repo / ".codeprobe" / "exp1").exists()
        assert (repo / "src" / "app.py").exists()

    def test_sweeps_stale_mcp_tempfiles(
        self, runner: CliRunner, tmp_path: Path, fake_tempdir: Path
    ) -> None:
        repo = _seed_repo(tmp_path)
        stale = fake_tempdir / "codeprobe-mcp-abcd1234.json"
        stale.write_text('{"mcpServers": {}}')
        unrelated = fake_tempdir / "unrelated.json"
        unrelated.write_text("{}")

        result = runner.invoke(purge, [str(repo), "--yes"])

        assert result.exit_code == 0, result.output
        assert not stale.exists()
        assert unrelated.exists()


class TestPurgeGuard:
    def test_refuses_outside_codeprobe_dir(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """A symlink inside a runs/ tree escaping .codeprobe/ aborts the
        whole purge (fail closed): nothing is deleted, non-zero exit."""
        repo = _seed_repo(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "precious.txt").write_text("customer data")
        runs = repo / ".codeprobe" / "exp1" / "runs"
        (runs / "base" / "escape").symlink_to(outside)

        result = runner.invoke(purge, [str(repo), "--yes"])

        assert result.exit_code != 0, result.output
        assert isinstance(result.exception, DiagnosticError)
        assert result.exception.code == "PURGE_REFUSED"
        assert (outside / "precious.txt").exists()
        # Fail closed: the refused candidate tree survives untouched.
        assert (runs / "trace.db").exists()
        assert (runs / "base" / "t1" / "agent_output.txt").exists()


class TestPurgeDisclosure:
    def test_help_carries_disclosure(self, runner: CliRunner) -> None:
        result = runner.invoke(purge, ["--help"])

        assert result.exit_code == 0, result.output
        assert "cleartext" in result.output.lower()

    def test_registered_on_root_cli(self, runner: CliRunner) -> None:
        from codeprobe.cli import main

        result = runner.invoke(main, ["--help"])

        assert result.exit_code == 0, result.output
        assert "purge" in result.output

    def test_readme_documents_data_at_rest(self) -> None:
        readme = _README_PATH.read_text(encoding="utf-8")

        assert "## Data at rest" in readme
        assert "codeprobe purge" in readme
