"""Tests for isolation primitives — multi-repo workspace setup."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codeprobe.core.isolation import (
    RepoRef,
    cleanup_multi_repo_workspace,
    quarantine_local_source,
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

        class _BoomError(RuntimeError):
            pass

        with pytest.raises(_BoomError):
            with quarantine_sibling_experiments(repo, active):
                assert not sibling_sentinel.exists()
                raise _BoomError("agent crashed")

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

        # Lay out repo/.codeprobe/experiment.json + tasks/task-001/. The repo
        # must be a real git checkout: every run path now executes inside a
        # worktree slot (codeprobe-f7rl.2).
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(
            ["git", "init", "-q", "-b", "main"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        for cfg in (["user.email", "t@t"], ["user.name", "t"]):
            subprocess.run(
                ["git", "config", *cfg], cwd=repo, check=True, capture_output=True
            )
        (repo / "README.md").write_text("seed\n")
        subprocess.run(
            ["git", "add", "README.md"], cwd=repo, check=True, capture_output=True
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", "seed"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
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


# ---------------------------------------------------------------------------
# quarantine_local_source — sg-only file-removal-and-bring-back (codeprobe-jf28)
# ---------------------------------------------------------------------------


class TestQuarantineLocalSource:
    """Cover the file-removal-and-bring-back primitive.

    Mirrors CSB's ``Dockerfile.sg_only`` and EB's
    ``generate_sg_only_dockerfile`` pattern: workspace appears empty
    during the yield window so the agent must use Sourcegraph MCP for
    code access, then source is restored afterwards.
    """

    def test_stashes_and_restores_top_level_entries(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "src").mkdir()
        (ws / "src" / "main.py").write_text("print('hi')")
        (ws / "README.md").write_text("readme")

        with quarantine_local_source(ws):
            # Workspace appears empty (apart from default-keep entries
            # which weren't created in this fixture).
            entries = sorted(p.name for p in ws.iterdir())
            assert entries == [], f"expected empty workspace, got {entries}"

        # Restored — original layout intact.
        assert (ws / "src" / "main.py").read_text() == "print('hi')"
        assert (ws / "README.md").read_text() == "readme"

    def test_keeps_dot_git_during_quarantine(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".git").mkdir()
        (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        (ws / "src.py").write_text("x = 1")

        with quarantine_local_source(ws):
            assert (ws / ".git").is_dir()
            assert (ws / ".git" / "HEAD").read_text() == "ref: refs/heads/main\n"
            assert not (ws / "src.py").exists()

        assert (ws / "src.py").read_text() == "x = 1"

    def test_keeps_codeprobe_metadata(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".codeprobe").mkdir()
        (ws / ".codeprobe" / "marker").write_text("M")
        (ws / ".codeprobe-worktrees-foo").mkdir()
        (ws / "lib.py").write_text("x")

        with quarantine_local_source(ws):
            assert (ws / ".codeprobe" / "marker").read_text() == "M"
            assert (ws / ".codeprobe-worktrees-foo").is_dir()
            assert not (ws / "lib.py").exists()

        assert (ws / "lib.py").read_text() == "x"

    def test_extra_keep_entries_survive(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "tests").mkdir()
        (ws / "tests" / "test.sh").write_text("#!/bin/sh\necho ok")
        (ws / "src.py").write_text("x")

        with quarantine_local_source(ws, keep=("tests",)):
            assert (ws / "tests" / "test.sh").read_text() == "#!/bin/sh\necho ok"
            assert not (ws / "src.py").exists()

        assert (ws / "src.py").read_text() == "x"

    def test_agent_created_files_survive_restore(self, tmp_path: Path) -> None:
        """Files the agent writes during the yield window stay in place."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "src.py").write_text("source")

        with quarantine_local_source(ws):
            # Simulate agent producing answer.txt in the empty workspace.
            (ws / "answer.txt").write_text("agent output")

        # Source restored AND answer.txt survives.
        assert (ws / "src.py").read_text() == "source"
        assert (ws / "answer.txt").read_text() == "agent output"

    def test_agent_overwriting_stashed_name_keeps_agent_version(
        self, tmp_path: Path
    ) -> None:
        """If the agent creates a same-named entry, its version wins."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "notes.md").write_text("original")

        with quarantine_local_source(ws):
            (ws / "notes.md").write_text("agent rewrote this")

        assert (ws / "notes.md").read_text() == "agent rewrote this"

    def test_restores_on_exception(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "src.py").write_text("source")

        with pytest.raises(RuntimeError, match="boom"):
            with quarantine_local_source(ws):
                assert not (ws / "src.py").exists()
                raise RuntimeError("boom")

        # Source restored despite the exception.
        assert (ws / "src.py").read_text() == "source"

    def test_no_op_when_workspace_missing(self, tmp_path: Path) -> None:
        missing = tmp_path / "does-not-exist"
        with quarantine_local_source(missing):
            # Should yield without raising; nothing to stash.
            pass

    def test_no_op_when_only_keep_entries_present(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / ".git").mkdir()

        with quarantine_local_source(ws):
            # .git is in default keep set; no quarantine activity.
            assert (ws / ".git").is_dir()

        assert (ws / ".git").is_dir()

    def test_stash_dir_cleaned_up_on_exit(self, tmp_path: Path) -> None:
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "src.py").write_text("x")

        with quarantine_local_source(ws):
            pass

        # No leftover stash dirs in the parent.
        leftover = [
            p
            for p in tmp_path.iterdir()
            if p.name.startswith(".codeprobe-source-stash-")
        ]
        assert leftover == [], f"stash leaked: {leftover}"

    def test_hide_mode_default_creates_no_scaffolds(self, tmp_path: Path) -> None:
        """Regression: default hide mode must NOT leave 0-byte placeholders.

        Scaffold mode (codeprobe-2nw2) adds a new branch to
        ``quarantine_local_source``. The default must remain byte-
        identical to the pre-scaffold codeprobe-jf28 behaviour.
        """
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "src").mkdir()
        (ws / "src" / "a.py").write_text("ORIG")
        (ws / "src" / "b.go").write_text("ORIG")

        observed_during_yield: list[str] = []
        with quarantine_local_source(ws):  # default mode="hide"
            observed_during_yield = sorted(p.name for p in ws.iterdir())

        # Default hide mode: workspace empty during yield, no
        # placeholders created at the original paths.
        assert observed_during_yield == [], (
            f"hide mode unexpectedly created placeholders: {observed_during_yield}"
        )
        # And no manifest was written anywhere.
        leftover_stash = [
            p for p in tmp_path.iterdir()
            if p.name.startswith(".codeprobe-source-stash-")
        ]
        assert leftover_stash == []
        # Source restored to original content (sanity).
        assert (ws / "src" / "a.py").read_text() == "ORIG"
        assert (ws / "src" / "b.go").read_text() == "ORIG"


def _stage_edit(path: Path, filename: str, content: str) -> None:
    """Modify a tracked file and stage it (mimics an agent's git add)."""
    (path / filename).write_text(content)
    subprocess.run(["git", "add", filename], cwd=path, check=True, capture_output=True)


class TestPinRobustToLeftoverState:
    """git_pin_commit + git_restore_clean must survive a pooled worktree that
    a prior uncapped agent left dirty / staged (codeprobe-9tk regression)."""

    def test_pin_forces_past_staged_changes(self, tmp_path: Path) -> None:
        from codeprobe.core.isolation import git_pin_commit

        repo = tmp_path / "repo"
        shas = _init_repo(repo, "c1", "c2", "c3")
        # Pin to c1, then mimic an agent staging a tracked-file edit.
        git_pin_commit(repo, shas[0])
        _stage_edit(repo, "file-0.txt", "agent edit, staged but not committed")
        # Re-pinning to a different commit must NOT abort on the staged change.
        git_pin_commit(repo, shas[2])
        assert _current_sha(repo) == shas[2]
        # The staged edit is gone — workspace matches the pinned commit.
        assert (repo / "file-0.txt").read_text() == "c1"

    def test_pin_forces_past_unstaged_changes(self, tmp_path: Path) -> None:
        from codeprobe.core.isolation import git_pin_commit

        repo = tmp_path / "repo"
        shas = _init_repo(repo, "c1", "c2")
        git_pin_commit(repo, shas[0])
        (repo / "file-0.txt").write_text("dirty unstaged")
        git_pin_commit(repo, shas[1])
        assert _current_sha(repo) == shas[1]

    def test_restore_clean_unstages_tracked_changes(self, tmp_path: Path) -> None:
        from codeprobe.core.isolation import git_restore_clean

        repo = tmp_path / "repo"
        _init_repo(repo, "c1", "c2")
        _stage_edit(repo, "file-0.txt", "staged edit")
        (repo / "untracked.txt").write_text("untracked")
        git_restore_clean(repo)
        # Both the staged tracked edit and the untracked file are gone.
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert status == ""
        # file-0.txt was committed as "c1" and never changed since; restoring
        # reverts the staged edit back to that committed content.
        assert (repo / "file-0.txt").read_text() == "c1"
        assert not (repo / "untracked.txt").exists()
