"""Tests for the ``--out`` flag on ``codeprobe mine`` (codeprobe-xcue).

BUG-OUT-FLAG-002 requires ``mine``/``run``/``interpret`` to all accept
``--out`` for custom output paths. This module covers ``mine``: the
``--help`` surface check plus functional redirection (tasks/ and
experiment.json land under ``--out`` instead of ``<repo>/.codeprobe``, and
omitting the flag keeps the pre-existing default location byte-identical).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main


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
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _mine_probes(repo: Path, *extra_args: str):
    """Invoke ``codeprobe mine --task-type micro_probe`` on *repo*."""
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


class TestMineOutHelp:
    def test_mine_help_contains_out(self) -> None:
        """BUG-OUT-FLAG-002: `codeprobe mine --help` must advertise --out."""
        result = CliRunner().invoke(main, ["mine", "--help"])
        assert result.exit_code == 0, result.output
        assert "--out" in result.output


class TestMineOutDefaultUnaffected:
    def test_omitting_out_keeps_default_location(self, tmp_path: Path) -> None:
        """Default behavior is byte-identical when --out is omitted."""
        repo = _make_probe_repo(tmp_path)
        result = _mine_probes(repo)
        assert result.exit_code == 0, result.output

        default_tasks_dir = repo / ".codeprobe" / "tasks"
        default_experiment = repo / ".codeprobe" / "experiment.json"
        assert default_tasks_dir.is_dir()
        assert any(default_tasks_dir.iterdir())
        assert default_experiment.is_file()


class TestMineOutRedirectsOutput:
    def test_out_redirects_tasks_and_experiment(self, tmp_path: Path) -> None:
        """--out redirects both tasks/ and experiment.json, and the repo's
        own .codeprobe/tasks stays empty (never populated)."""
        repo = _make_probe_repo(tmp_path)
        out_dir = tmp_path / "custom-out"
        out_dir.mkdir()

        result = _mine_probes(repo, "--out", str(out_dir))
        assert result.exit_code == 0, result.output

        custom_tasks_dir = out_dir / "tasks"
        custom_experiment = out_dir / "experiment.json"
        assert custom_tasks_dir.is_dir()
        assert any(custom_tasks_dir.iterdir())
        assert custom_experiment.is_file()

        experiment_data = json.loads(custom_experiment.read_text())
        assert experiment_data["task_ids"]

        default_tasks_dir = repo / ".codeprobe" / "tasks"
        default_experiment = repo / ".codeprobe" / "experiment.json"
        assert not default_tasks_dir.exists()
        assert not default_experiment.exists()

    def test_out_json_envelope_reports_tasks_dir(self, tmp_path: Path) -> None:
        repo = _make_probe_repo(tmp_path)
        out_dir = tmp_path / "custom-out"
        out_dir.mkdir()

        result = _mine_probes(repo, "--out", str(out_dir), "--json")
        assert result.exit_code == 0, result.output
        envelope = json.loads(
            [ln for ln in result.output.splitlines() if ln.strip()][-1]
        )
        assert envelope["data"]["tasks_dir"] == str(out_dir / "tasks")


class TestMineOutValidation:
    def test_out_rejects_missing_parent_directory(self, tmp_path: Path) -> None:
        repo = _make_probe_repo(tmp_path)
        bad_out = tmp_path / "does-not-exist" / "out"

        result = _mine_probes(repo, "--out", str(bad_out))
        assert result.exit_code != 0
        assert "does not exist" in result.output

    def test_out_rejected_with_refresh(self, tmp_path: Path) -> None:
        repo = _make_probe_repo(tmp_path)
        out_dir = tmp_path / "custom-out"
        out_dir.mkdir()

        result = CliRunner().invoke(
            main,
            [
                "mine",
                str(repo),
                "--refresh",
                str(repo),
                "--out",
                str(out_dir),
            ],
        )
        assert result.exit_code != 0
        assert "Cannot use --out with --refresh" in result.output
