"""Tests for isolation primitives — multi-repo workspace setup."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from codeprobe.core.isolation import (
    RepoRef,
    cleanup_multi_repo_workspace,
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
