"""Tests for isolation primitives — multi-repo workspace setup."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codeprobe.core.isolation import (
    RepoRef,
    cleanup_multi_repo_workspace,
    quarantine_sibling_experiments,
    setup_multi_repo_workspace,
)


def _init_repo(path: Path, *commits: str) -> list[str]:
    """Initialize a git repo at *path* with the given commit messages.

    Returns the list of commit SHAs in order.
    """
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, check=True)
    shas: list[str] = []
    for i, msg in enumerate(commits):
        (path / f"file-{i}.txt").write_text(msg)
        subprocess.run(["git", "add", "."], cwd=path, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", msg],
            cwd=path,
            check=True,
            capture_output=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        shas.append(sha)
    return shas


def _current_sha(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class TestRepoRef:
    def test_requires_name(self) -> None:
        with pytest.raises(ValueError, match="name"):
            RepoRef(name="", ground_truth_commit="abc", local_path="/tmp")

    def test_requires_ground_truth_commit(self) -> None:
        with pytest.raises(ValueError, match="ground_truth_commit"):
            RepoRef(name="foo", ground_truth_commit="", local_path="/tmp")

    def test_requires_url_or_local_path(self) -> None:
        with pytest.raises(ValueError, match="url or local_path"):
            RepoRef(name="foo", ground_truth_commit="abc")

    def test_frozen(self) -> None:
        ref = RepoRef(name="foo", ground_truth_commit="abc", local_path="/tmp")
        with pytest.raises(Exception):  # FrozenInstanceError
            ref.name = "bar"  # type: ignore[misc]


class TestMultiRepoWorkspace:
    def test_pins_two_local_repos_to_individual_commits(self, tmp_path: Path) -> None:
        """Each secondary repo is pinned to its own ground_truth_commit^."""
        repo_a = tmp_path / "src_a"
        repo_b = tmp_path / "src_b"
        shas_a = _init_repo(repo_a, "a1", "a2", "a3")
        shas_b = _init_repo(repo_b, "b1", "b2")

        workspace = tmp_path / "ws"
        workspace.mkdir()

        refs = [
            RepoRef(
                name="repoA",
                ground_truth_commit=shas_a[2],  # parent = shas_a[1]
                local_path=str(repo_a),
            ),
            RepoRef(
                name="repoB",
                ground_truth_commit=shas_b[1],  # parent = shas_b[0]
                local_path=str(repo_b),
            ),
        ]
        paths = setup_multi_repo_workspace(workspace, refs)

        assert paths == [workspace / "repos" / "repoA", workspace / "repos" / "repoB"]
        assert _current_sha(workspace / "repos" / "repoA") == shas_a[1]
        assert _current_sha(workspace / "repos" / "repoB") == shas_b[0]

    def test_accepts_dict_shape(self, tmp_path: Path) -> None:
        repo = tmp_path / "src"
        shas = _init_repo(repo, "c1", "c2")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        setup_multi_repo_workspace(
            workspace,
            [
                {
                    "name": "repoX",
                    "ground_truth_commit": shas[1],
                    "local_path": str(repo),
                }
            ],
        )
        assert _current_sha(workspace / "repos" / "repoX") == shas[0]

    def test_failure_mid_setup_rolls_back(self, tmp_path: Path) -> None:
        """If repo 2 fails, repo 1 must not be left behind."""
        repo_ok = tmp_path / "ok"
        _init_repo(repo_ok, "ok1", "ok2")
        workspace = tmp_path / "ws"
        workspace.mkdir()

        # Second ref has an unreachable commit → git checkout fails.
        refs = [
            RepoRef(
                name="repoOK",
                ground_truth_commit=_current_sha(repo_ok),
                local_path=str(repo_ok),
            ),
            RepoRef(
                name="repoBAD",
                ground_truth_commit="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
                local_path=str(repo_ok),
            ),
        ]
        with pytest.raises(subprocess.CalledProcessError):
            setup_multi_repo_workspace(workspace, refs)

        # Rollback: neither repo dir should remain
        assert not (workspace / "repos" / "repoOK").exists()
        assert not (workspace / "repos" / "repoBAD").exists()

    def test_failure_on_missing_local_path(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        with pytest.raises(FileNotFoundError):
            setup_multi_repo_workspace(
                workspace,
                [
                    RepoRef(
                        name="missing",
                        ground_truth_commit="abc",
                        local_path=str(tmp_path / "does-not-exist"),
                    )
                ],
            )
        assert not (workspace / "repos" / "missing").exists()


class TestCleanupMultiRepoWorkspace:
    def test_removes_repos_dir(self, tmp_path: Path) -> None:
        (tmp_path / "repos" / "a").mkdir(parents=True)
        (tmp_path / "repos" / "a" / "file.txt").write_text("x")
        cleanup_multi_repo_workspace(tmp_path)
        assert not (tmp_path / "repos").exists()

    def test_noop_when_absent(self, tmp_path: Path) -> None:
        # Should not raise
        cleanup_multi_repo_workspace(tmp_path)


def _make_experiment_dir(repo: Path, name: str, sentinel: str) -> Path:
    """Create a fake top-level experiment dir with experiment.json + sentinel."""
    exp_dir = repo / name
    exp_dir.mkdir(parents=True)
    (exp_dir / "experiment.json").write_text(json.dumps({"name": name}))
    (exp_dir / "ground_truth.json").write_text(sentinel)
    return exp_dir


class TestQuarantineSiblingExperiments:
    """Regression — see codeprobe-gy5p (gascity ground-truth leak 2026-04-25)."""

    def test_sibling_hidden_during_block_and_restored_after(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        active = _make_experiment_dir(repo, ".codeprobe", "ACTIVE")
        sibling = _make_experiment_dir(repo, ".codeprobe-other", "LEAKED")

        sibling_sentinel = sibling / "ground_truth.json"
        active_sentinel = active / "ground_truth.json"

        with quarantine_sibling_experiments(repo, active):
            # Active dir is still readable.
            assert active_sentinel.read_text() == "ACTIVE"
            # Sibling sentinel is gone for the duration of the run.
            assert not sibling_sentinel.exists()
            with pytest.raises(FileNotFoundError):
                sibling_sentinel.open()

        # Restored after block.
        assert sibling_sentinel.exists()
        assert sibling_sentinel.read_text() == "LEAKED"
        assert active_sentinel.read_text() == "ACTIVE"

    def test_sibling_restored_on_exception(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        active = _make_experiment_dir(repo, ".codeprobe", "ACTIVE")
        sibling = _make_experiment_dir(repo, ".codeprobe-other", "LEAKED")
        sibling_sentinel = sibling / "ground_truth.json"

        class _Boom(RuntimeError):
            pass

        with pytest.raises(_Boom):
            with quarantine_sibling_experiments(repo, active):
                assert not sibling_sentinel.exists()
                raise _Boom("agent crashed")

        # Sibling restored even though the with-block exited via exception.
        assert sibling_sentinel.exists()
        assert sibling_sentinel.read_text() == "LEAKED"

    def test_active_dir_preserved_when_it_has_experiment_json(
        self, tmp_path: Path
    ) -> None:
        """Top-level active dir (Case A: .codeprobe/experiment.json) must NOT
        be quarantined — that would break the run we're trying to protect.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        active = _make_experiment_dir(repo, ".codeprobe", "ACTIVE")

        with quarantine_sibling_experiments(repo, active):
            assert (active / "experiment.json").is_file()
            assert (active / "ground_truth.json").read_text() == "ACTIVE"

    def test_no_siblings_is_noop(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        active = _make_experiment_dir(repo, ".codeprobe", "ACTIVE")

        # No siblings — no quarantine dir should be created.
        with quarantine_sibling_experiments(repo, active):
            quarantine_dirs = [
                p
                for p in repo.iterdir()
                if p.name.startswith(".codeprobe-quarantine-")
            ]
            assert quarantine_dirs == []

    def test_quarantine_dir_removed_after_block(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        active = _make_experiment_dir(repo, ".codeprobe", "ACTIVE")
        _make_experiment_dir(repo, ".codeprobe-other", "LEAKED")

        with quarantine_sibling_experiments(repo, active):
            pass

        leftover = [
            p for p in repo.iterdir() if p.name.startswith(".codeprobe-quarantine-")
        ]
        assert leftover == []

    def test_active_dir_outside_repo_skips_quarantine(self, tmp_path: Path) -> None:
        """Defensive: if the active experiment dir doesn't resolve under the
        repo (unusual layout), don't blindly hide every top-level experiment
        dir — log a warning and yield without quarantining.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        sibling = _make_experiment_dir(repo, ".codeprobe-other", "LEAKED")
        outside = tmp_path / "outside-experiment"
        outside.mkdir()
        (outside / "experiment.json").write_text("{}")

        with quarantine_sibling_experiments(repo, outside):
            # Sibling is NOT moved because we cannot safely identify the
            # active top-level component.
            assert (sibling / "ground_truth.json").exists()


class TestExecuteConfigQuarantinesSiblings:
    """End-to-end regression — execute_config must wire the quarantine.

    Reproduces the codeprobe-gy5p leak: a stub agent inspecting the repo root
    during ``run()`` MUST NOT see another experiment's ground_truth.json.
    """

    def test_sibling_hidden_during_dispatch_and_restored_after(
        self, tmp_path: Path
    ) -> None:
        import stat

        from codeprobe.adapters.protocol import AgentConfig, AgentOutput
        from codeprobe.core.executor import execute_config
        from codeprobe.models.experiment import ExperimentConfig

        # Lay out repo/.codeprobe/experiment.json + tasks/task-001/
        repo = tmp_path / "repo"
        repo.mkdir()
        active_exp = repo / ".codeprobe"
        active_exp.mkdir()
        (active_exp / "experiment.json").write_text("{}")
        task_dir = active_exp / "tasks" / "task-001"
        task_dir.mkdir(parents=True)
        (task_dir / "instruction.md").write_text("Do the thing.")
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        test_sh = tests_dir / "test.sh"
        test_sh.write_text("#!/bin/bash\nexit 0\n")
        test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)

        # Sibling experiment dir at the repo root with the leaking sentinel.
        sibling = _make_experiment_dir(repo, ".codeprobe-other", "LEAKED")
        sibling_sentinel = sibling / "ground_truth.json"

        observations: dict[str, bool] = {}

        class _PeekingAdapter:
            name = "fake-peeker"
            run_calls: list[tuple[str, object]] = []

            def find_binary(self) -> str | None:
                return "/usr/bin/fake-agent"

            def preflight(self, config: AgentConfig) -> list[str]:
                return []

            def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
                return ["fake-agent"]

            def run(
                self,
                prompt: str,
                config: AgentConfig,
                session_env: dict[str, str] | None = None,
            ) -> AgentOutput:
                # Inspect the sibling sentinel from inside the "agent run".
                observations["sibling_visible_during_run"] = sibling_sentinel.exists()
                return AgentOutput(
                    stdout="ok",
                    stderr=None,
                    exit_code=0,
                    duration_seconds=0.1,
                )

            def isolate_session(self, slot_id: int) -> dict[str, str]:
                return {}

        adapter = _PeekingAdapter()

        results = execute_config(
            adapter=adapter,
            task_dirs=[task_dir],
            repo_path=repo,
            experiment_config=ExperimentConfig(label="baseline"),
            agent_config=AgentConfig(),
        )

        assert len(results) == 1
        assert observations.get("sibling_visible_during_run") is False, (
            "sibling experiment dir was visible to the agent during run() — "
            "quarantine did not activate"
        )
        # Sibling restored after dispatch.
        assert sibling_sentinel.exists()
        assert sibling_sentinel.read_text() == "LEAKED"
