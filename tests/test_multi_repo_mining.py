"""Tests for cross-repo mining (bead codeprobe-aev).

Covers:
- TaskMetadata.additional_repos round-trip through metadata.json
- RipgrepResolver finds symbol references across checked-out repos
- mine_tasks_multi() produces a cross-repo task with file_list ground truth
- refactor / dependency families raise NotImplementedError
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import asdict
from pathlib import Path

import pytest

from codeprobe.mining.multi_repo import (
    RipgrepResolver,
    SymbolResolver,
    mine_tasks_multi,
)
from codeprobe.models.task import RepoRef, Task, TaskMetadata

# ---------------------------------------------------------------------------
# TaskMetadata round-trip
# ---------------------------------------------------------------------------


def test_task_metadata_additional_repos_default_empty() -> None:
    meta = TaskMetadata(name="t1")
    assert meta.additional_repos == ()


def test_task_metadata_additional_repos_round_trip(tmp_path: Path) -> None:
    repos = (
        RepoRef(name="lib", ground_truth_commit="abc123", url="https://x/lib.git"),
        RepoRef(name="svc", ground_truth_commit="def456", local_path="/tmp/svc"),
    )
    meta = TaskMetadata(name="t1", additional_repos=repos)
    task = Task(id="t1", repo="primary", metadata=meta)

    # Serialize via asdict (same path as writer.write_task_dir)
    data = asdict(task)
    out = tmp_path / "metadata.json"
    out.write_text(json.dumps(data, indent=2))

    loaded = json.loads(out.read_text())
    assert len(loaded["metadata"]["additional_repos"]) == 2
    assert loaded["metadata"]["additional_repos"][0]["name"] == "lib"
    assert loaded["metadata"]["additional_repos"][0]["ground_truth_commit"] == "abc123"
    assert loaded["metadata"]["additional_repos"][1]["local_path"] == "/tmp/svc"


# ---------------------------------------------------------------------------
# Two-repo fixture with known cross-references
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


@pytest.fixture
def two_repo_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Primary repo defines `calculate_total`; secondary imports and calls it."""
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()

    # Primary: define the public function
    (primary / "lib.py").write_text(
        "def calculate_total(items):\n    return sum(items)\n"
    )
    (primary / "unrelated.py").write_text("x = 1\n")
    _git(primary, "init", "-q", "-b", "main")
    _git(primary, "add", ".")
    _git(primary, "commit", "-q", "-m", "initial")

    # Modify calculate_total in a second commit (simulates a PR)
    (primary / "lib.py").write_text(
        "def calculate_total(items, tax=0):\n" "    return sum(items) * (1 + tax)\n"
    )
    _git(primary, "add", ".")
    _git(primary, "commit", "-q", "-m", "feat: add tax to calculate_total")

    # Secondary: imports and calls it from two files
    (secondary / "caller_a.py").write_text(
        "from lib import calculate_total\n\nprint(calculate_total([1, 2]))\n"
    )
    (secondary / "caller_b.py").write_text(
        "import lib\n\nprint(lib.calculate_total([3, 4]))\n"
    )
    (secondary / "noise.py").write_text("# nothing to see here\n")
    _git(secondary, "init", "-q", "-b", "main")
    _git(secondary, "add", ".")
    _git(secondary, "commit", "-q", "-m", "initial")

    return primary, secondary


# ---------------------------------------------------------------------------
# RipgrepResolver
# ---------------------------------------------------------------------------


def test_ripgrep_resolver_finds_cross_repo_references(
    two_repo_fixture: tuple[Path, Path],
) -> None:
    _, secondary = two_repo_fixture
    resolver = RipgrepResolver()
    refs = resolver.find_references("calculate_total", [str(secondary)])
    files = {Path(r.path).name for r in refs}
    assert "caller_a.py" in files
    assert "caller_b.py" in files
    assert "noise.py" not in files


def test_ripgrep_resolver_no_matches(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "x.py").write_text("pass\n")
    resolver = RipgrepResolver()
    refs = resolver.find_references("nonexistent_symbol", [str(empty)])
    assert refs == []


def test_ripgrep_resolver_implements_protocol() -> None:
    resolver: SymbolResolver = RipgrepResolver()
    assert hasattr(resolver, "find_references")
    assert hasattr(resolver, "resolve_symbol_at")


# ---------------------------------------------------------------------------
# mine_tasks_multi — callers family
# ---------------------------------------------------------------------------


def test_mine_tasks_multi_callers_produces_cross_repo_task(
    two_repo_fixture: tuple[Path, Path], tmp_path: Path
) -> None:
    primary, secondary = two_repo_fixture
    resolver = RipgrepResolver()

    result = mine_tasks_multi(
        primary,
        (secondary,),
        count=5,
        family="callers",
        symbol_resolver=resolver,
    )

    assert len(result.tasks) >= 1
    task = result.tasks[0]
    # Cross-repo task should record secondary in additional_repos
    assert len(task.metadata.additional_repos) >= 1
    assert task.metadata.additional_repos[0].name == "secondary"
    # Ground truth should be a file_list spanning secondary
    gt_files = result.ground_truth_files[task.id]
    assert any("caller_a.py" in f for f in gt_files)
    assert task.verification.oracle_type == "file_list"


def test_mine_tasks_multi_refactor_not_implemented(
    two_repo_fixture: tuple[Path, Path],
) -> None:
    primary, secondary = two_repo_fixture
    with pytest.raises(NotImplementedError, match="refactor"):
        mine_tasks_multi(
            primary,
            (secondary,),
            count=1,
            family="refactor",
            symbol_resolver=RipgrepResolver(),
        )


def test_mine_tasks_multi_dependency_not_implemented(
    two_repo_fixture: tuple[Path, Path],
) -> None:
    primary, secondary = two_repo_fixture
    with pytest.raises(NotImplementedError, match="dependency"):
        mine_tasks_multi(
            primary,
            (secondary,),
            count=1,
            family="dependency",
            symbol_resolver=RipgrepResolver(),
        )


# ---------------------------------------------------------------------------
# Sourcegraph-backed resolver (integration, skipped without env token)
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_sourcegraph_resolver_find_references() -> None:
    import os

    token = os.environ.get("SOURCEGRAPH_TOKEN")
    if not token:
        pytest.skip("SOURCEGRAPH_TOKEN not set")
    from codeprobe.mining.sg_ground_truth import SourcegraphSymbolResolver

    resolver = SourcegraphSymbolResolver(
        defining_file="src/lib.py",
    )
    refs = resolver.find_references("os", ["github.com/python/cpython"])
    # Integration smoke: at least returns a list
    assert isinstance(refs, list)
