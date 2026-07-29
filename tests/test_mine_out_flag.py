"""Tests for the ``--out`` flag on ``codeprobe mine`` (codeprobe-xcue).

BUG-OUT-FLAG-002 requires ``mine``/``run``/``interpret`` to all accept
``--out`` for custom output paths. This module covers ``mine``: the
``--help`` surface check plus functional redirection (tasks/ and
experiment.json land under ``--out`` instead of ``<repo>/.codeprobe``, and
omitting the flag keeps the pre-existing default location byte-identical).
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.core.experiment import save_experiment
from codeprobe.models.experiment import Experiment
from codeprobe.models.task import Task, TaskMetadata, TaskVerification


def _make_merge_pr_repo(base: Path, *, n_prs: int = 3) -> Path:
    """Create a repo with *n_prs* merged PRs, each with a test file whose
    name overlaps its source file (needed for SDLC extraction + quality
    scoring — see ``score_pr_quality`` signal 3) and enough non-test
    symbols for probe generation (the ``mixed`` dispatch path).
    """
    repo = base / "sdlcrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    for cfg in (["user.email", "test@example.com"], ["user.name", "test"]):
        subprocess.run(["git", "config", *cfg], cwd=repo, check=True)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    (repo / "calc.py").write_text(
        '"""Small calculator module."""\n'
        "\n"
        "\n"
        "def add(a: int, b: int) -> int:\n"
        '    """Return the sum of a and b."""\n'
        "    return a + b\n"
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "chore: init"], cwd=repo, check=True)

    for i in range(1, n_prs + 1):
        branch = f"pr/{i}"
        subprocess.run(["git", "checkout", "-qb", branch], cwd=repo, check=True)
        src = repo / f"src_{i}.py"
        src.write_text(f"def feature_{i}(x):\n    return x + {i}\n")
        tests_dir = repo / "tests"
        tests_dir.mkdir(exist_ok=True)
        (tests_dir / f"test_src_{i}.py").write_text(
            f"from src_{i} import feature_{i}\n\n\n"
            f"def test_feature_{i}():\n    assert feature_{i}(1) == {i + 1}\n"
        )
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-qm", f"feat: add feature {i}"], cwd=repo, check=True
        )
        subprocess.run(["git", "checkout", "-q", "main"], cwd=repo, check=True)
        subprocess.run(
            [
                "git", "merge", "--no-ff", "-q", "-m",
                f"Merge PR #{i}: add feature {i}", branch,
            ],
            cwd=repo,
            check=True,
        )
    return repo


# --min-quality: local (no gh/remote) mining can only ever earn the
# "test file name overlaps source file" structural signal (0.25) — the
# issue-reference and PR-body signals both require a fetched PR body,
# unavailable for these local-only fixtures. See score_pr_quality.
_LOCAL_MIN_QUALITY = "0.2"


def _mine_local_pr_repo(repo: Path, task_type: str, *extra_args: str):
    """Invoke ``codeprobe mine --task-type <task_type>`` on a local-only
    merge-PR repo, with the flags that make its 0.25-quality-ceiling
    tasks and no-PR-narrative history mineable.
    """
    return CliRunner().invoke(
        main,
        [
            "mine",
            str(repo),
            "--task-type",
            task_type,
            "--no-interactive",
            "--no-llm",
            "--min-quality",
            _LOCAL_MIN_QUALITY,
            "--narrative-source",
            "commits",
            *extra_args,
        ],
    )


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


class TestMineOutSdlcDispatch:
    """codeprobe-xcue Finding 4: --out coverage for the ``sdlc`` dispatch
    path (``_dispatch_sdlc``), not just ``micro_probe``.
    """

    def test_out_redirects_sdlc_tasks_and_experiment(self, tmp_path: Path) -> None:
        repo = _make_merge_pr_repo(tmp_path, n_prs=3)
        out_dir = tmp_path / "custom-sdlc-out"
        out_dir.mkdir()

        result = _mine_local_pr_repo(
            repo, "sdlc_code_change", "--out", str(out_dir)
        )
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

    def test_omitting_out_keeps_default_location(self, tmp_path: Path) -> None:
        repo = _make_merge_pr_repo(tmp_path, n_prs=3)
        result = _mine_local_pr_repo(repo, "sdlc_code_change")
        assert result.exit_code == 0, result.output

        default_tasks_dir = repo / ".codeprobe" / "tasks"
        default_experiment = repo / ".codeprobe" / "experiment.json"
        assert default_tasks_dir.is_dir()
        assert any(default_tasks_dir.iterdir())
        assert default_experiment.is_file()

    def test_out_next_steps_target_out_dir_not_repo(self, tmp_path: Path) -> None:
        """codeprobe-xcue Finding 2: the printed 'Run the eval' command must
        target --out's location, not the (now-empty) repo/.codeprobe."""
        repo = _make_merge_pr_repo(tmp_path, n_prs=3)
        out_dir = tmp_path / "custom-sdlc-out"
        out_dir.mkdir()

        result = _mine_local_pr_repo(
            repo, "sdlc_code_change", "--out", str(out_dir)
        )
        assert result.exit_code == 0, result.output
        assert f"codeprobe run {out_dir} --agent claude" in result.output
        assert f"codeprobe run {repo} --agent claude" not in result.output

    def test_org_scale_out_keeps_repo_positional_and_passes_suite(
        self, tmp_path: Path
    ) -> None:
        from io import StringIO
        from unittest.mock import patch

        from codeprobe.cli.mine_cmd import _show_org_scale_results

        task = Task(
            id="org-001",
            repo="repo",
            metadata=TaskMetadata(
                name="org-task",
                difficulty="medium",
                category="migration-inventory",
                org_scale=True,
            ),
            verification=TaskVerification(
                oracle_type="file_list",
                oracle_answer=("a.py",),
            ),
        )
        repo = tmp_path / "repo"
        out_dir = tmp_path / "custom-org-out"
        repo.mkdir()
        out_dir.mkdir()
        tasks_dir = out_dir / "tasks"
        tasks_dir.mkdir()
        save_experiment(
            out_dir,
            Experiment(
                name="org-scale",
                tasks_dir="tasks",
                task_ids=(task.id,),
            ),
        )

        buf = StringIO()
        with patch(
            "click.echo", side_effect=lambda msg="", **kw: buf.write(msg + "\n")
        ):
            _show_org_scale_results([task], tasks_dir, repo, out_dir=out_dir)

        output = buf.getvalue()
        run_command = next(
            line.strip()
            for line in output.splitlines()
            if line.strip().startswith("codeprobe run ")
        )
        run_argv = shlex.split(run_command)
        assert run_argv[:3] == ["codeprobe", "run", str(repo)]
        assert run_argv[run_argv.index("--config") + 1] == str(out_dir)
        assert run_argv[run_argv.index("--suite") + 1] == str(
            out_dir / "suite.toml"
        )
        assert f"codeprobe run {out_dir} --agent claude" not in output

        executed = CliRunner().invoke(
            main,
            [*run_argv[1:], "--dry-run", "--json"],
        )
        combined = executed.output + (executed.stderr or "")
        assert "NO_EXPERIMENT" not in combined
        assert executed.exit_code != 0
        assert json.loads(executed.output)["error"]["code"] == "NO_TASKS"


class TestMineOutMixedDispatch:
    """codeprobe-xcue Finding 4: --out coverage for a second dispatch path
    (``mixed`` — SDLC mining + probe generation combined).
    """

    def test_out_redirects_mixed_tasks_and_experiment(self, tmp_path: Path) -> None:
        repo = _make_merge_pr_repo(tmp_path, n_prs=3)
        out_dir = tmp_path / "custom-mixed-out"
        out_dir.mkdir()

        result = _mine_local_pr_repo(repo, "mixed", "--out", str(out_dir))
        assert result.exit_code == 0, result.output

        custom_tasks_dir = out_dir / "tasks"
        custom_experiment = out_dir / "experiment.json"
        assert custom_tasks_dir.is_dir()
        assert any(custom_tasks_dir.iterdir())
        assert custom_experiment.is_file()

        default_tasks_dir = repo / ".codeprobe" / "tasks"
        default_experiment = repo / ".codeprobe" / "experiment.json"
        assert not default_tasks_dir.exists()
        assert not default_experiment.exists()

    def test_omitting_out_keeps_default_location(self, tmp_path: Path) -> None:
        repo = _make_merge_pr_repo(tmp_path, n_prs=3)
        result = _mine_local_pr_repo(repo, "mixed")
        assert result.exit_code == 0, result.output

        default_tasks_dir = repo / ".codeprobe" / "tasks"
        default_experiment = repo / ".codeprobe" / "experiment.json"
        assert default_tasks_dir.is_dir()
        assert any(default_tasks_dir.iterdir())
        assert default_experiment.is_file()
