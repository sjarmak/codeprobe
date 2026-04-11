"""Tests for u6: executor dual-mode + per-run isolation.

Covers:
- Verification-mode override forces reward_type='dual' regardless of caller
- 'answer.json' is removed from stale state pre-run
- Per-run scoring sandbox (task_dir is never mutated by scoring)
- Per-run worktree acquisition / release for dual tasks
- ScoreResult.details propagation into CompletedTask.scoring_details
- TaskScored event carries scoring_details
- Parallel stress (parallel=4 repeats=3 = 12 runs) with no cross-contamination
"""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from codeprobe.adapters.protocol import AgentConfig
from codeprobe.core.events import EventDispatcher, TaskScored
from codeprobe.core.executor import (
    TaskResult,
    execute_config,
    execute_task,
)
from codeprobe.core.isolation import IsolationStrategy, WorktreeIsolation
from codeprobe.core.scoring import ScoreResult
from codeprobe.models.experiment import ExperimentConfig
from tests.conftest import FakeAdapter

# ---------------------------------------------------------------------------
# Fakes / spies
# ---------------------------------------------------------------------------


class _FakeDualScorer:
    """Synchronous stub for DualScorer used in isolation tests."""

    def __init__(self) -> None:
        self.calls: list[Path] = []
        self._lock = threading.Lock()

    def score(self, agent_output: str, task_dir: Path) -> ScoreResult:
        with self._lock:
            self.calls.append(Path(task_dir))
        return ScoreResult(
            score=0.75,
            passed=True,
            details={
                "score_direct": 1.0,
                "score_artifact": 0.5,
                "scoring_policy": "weighted",
                "passed_direct": True,
                "passed_artifact": True,
            },
        )


class _SpyIsolation:
    """IsolationStrategy spy that records acquire/release calls.

    Wraps a real WorktreeIsolation so the worktree pool actually exists on
    disk and can be exercised end-to-end.
    """

    def __init__(self, repo_path: Path, namespace: str) -> None:
        self.acquired: list[Path] = []
        self.released: list[Path] = []
        self.cleaned = False
        self._inner = WorktreeIsolation(repo_path, pool_size=1, namespace=namespace)

    def acquire(self) -> Path:
        wt = self._inner.acquire()
        self.acquired.append(wt)
        return wt

    def reset(self, workspace: Path) -> None:
        self._inner.reset(workspace)

    def release(self, workspace: Path) -> None:
        self.released.append(workspace)
        self._inner.release(workspace)

    def cleanup(self) -> None:
        self.cleaned = True
        self._inner.cleanup()


class _CapturingListener:
    """RunEventListener that records every TaskScored event."""

    def __init__(self) -> None:
        self.events: list[TaskScored] = []
        self._lock = threading.Lock()

    def on_event(self, event: Any) -> None:
        if isinstance(event, TaskScored):
            with self._lock:
                self.events.append(event)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
    )
    # User identity required for the initial commit
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("seed\n")
    subprocess.run(
        ["git", "-C", str(repo), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
    )


def _make_dual_task(
    task_dir: Path,
    *,
    verification_mode: str = "dual",
    extra_files: dict[str, str] | None = None,
) -> Path:
    """Create a minimal dual-mode task directory."""
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text("Do dual.")

    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    test_sh = tests_dir / "test.sh"
    test_sh.write_text("#!/bin/bash\nexit 0\n")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)

    metadata = {
        "verification": {
            "verification_mode": verification_mode,
            "reward_type": "binary",
            "scoring_policy": "weighted",
            "weight_direct": 0.5,
            "weight_artifact": 0.5,
        }
    }
    (task_dir / "metadata.json").write_text(json.dumps(metadata))

    if extra_files:
        for name, content in extra_files.items():
            (task_dir / name).write_text(content)

    return task_dir


def _hash_dir(path: Path) -> str:
    """Compute a deterministic SHA256 over the file tree contents."""
    h = hashlib.sha256()
    for entry in sorted(path.rglob("*")):
        if entry.is_file():
            rel = entry.relative_to(path).as_posix()
            h.update(rel.encode("utf-8"))
            h.update(b"\0")
            h.update(entry.read_bytes())
            h.update(b"\0")
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_verification_mode_dual_overrides_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A task with verification_mode='dual' forces reward_type='dual'.

    Even when the experiment passes reward_type='binary'.
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    task = _make_dual_task(tmp_path / "task")

    fake = _FakeDualScorer()
    received: list[str] = []

    def _spy_get_scorer(rt: str):
        received.append(rt)
        return fake

    monkeypatch.setattr("codeprobe.core.executor.get_scorer", _spy_get_scorer)

    adapter = FakeAdapter()
    result = execute_task(
        adapter=adapter,
        task_dir=task,
        repo_path=repo,
        agent_config=AgentConfig(),
        reward_type="binary",
    )
    assert result.completed.status == "completed"
    assert received == ["dual"]
    assert len(fake.calls) == 1


def test_verification_mode_dual_overrides_continuous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override is at TOP LEVEL — not nested under reward_type=='binary'."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    task = _make_dual_task(tmp_path / "task")

    fake = _FakeDualScorer()
    received: list[str] = []

    def _spy_get_scorer(rt: str):
        received.append(rt)
        return fake

    monkeypatch.setattr("codeprobe.core.executor.get_scorer", _spy_get_scorer)

    adapter = FakeAdapter()
    result = execute_task(
        adapter=adapter,
        task_dir=task,
        repo_path=repo,
        agent_config=AgentConfig(),
        reward_type="continuous",  # NOT binary — override must still fire
    )
    assert result.completed.status == "completed"
    assert received == ["dual"]


def test_non_dual_verification_mode_does_not_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tasks without verification_mode='dual' must NOT be force-routed."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    task = _make_dual_task(tmp_path / "task", verification_mode="test_script")

    received: list[str] = []

    def _spy_get_scorer(rt: str):
        received.append(rt)
        return _FakeDualScorer()

    monkeypatch.setattr("codeprobe.core.executor.get_scorer", _spy_get_scorer)

    adapter = FakeAdapter()
    execute_task(
        adapter=adapter,
        task_dir=task,
        repo_path=repo,
        agent_config=AgentConfig(),
        reward_type="continuous",
    )
    assert received == ["continuous"]


def test_stale_answer_json_in_task_dir_is_preserved_but_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-existing ``answer.json`` in task_dir survives the run unchanged.

    The old behavior mutated task_dir at the start of every run. That
    raced with concurrent execute_task calls and could destroy legitimate
    fixture files. The new contract is: task_dir is NEVER mutated. Stale
    artifacts get scrubbed inside the per-run scoring sandbox after the
    snapshot copytree, not in the shared task_dir.
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    stale_content = '{"old": true}'
    task = _make_dual_task(
        tmp_path / "task",
        verification_mode="test_script",
        extra_files={"answer.json": stale_content},
    )
    assert (task / "answer.json").is_file()

    monkeypatch.setattr(
        "codeprobe.core.executor.get_scorer",
        lambda rt: _FakeDualScorer(),
    )

    adapter = FakeAdapter()
    execute_task(
        adapter=adapter,
        task_dir=task,
        repo_path=repo,
        agent_config=AgentConfig(),
        reward_type="binary",
    )
    # task_dir was not mutated — the stale file is still there with its
    # original contents.
    assert (task / "answer.json").is_file()
    assert (task / "answer.json").read_text(encoding="utf-8") == stale_content


def test_task_dir_unchanged_by_scoring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Per-run scoring sandbox: original task_dir SHA256 unchanged."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    task = _make_dual_task(tmp_path / "task")

    monkeypatch.setattr(
        "codeprobe.core.executor.get_scorer",
        lambda rt: _FakeDualScorer(),
    )

    before = _hash_dir(task)

    adapter = FakeAdapter()
    result = execute_task(
        adapter=adapter,
        task_dir=task,
        repo_path=repo,
        agent_config=AgentConfig(),
        reward_type="binary",
    )
    assert result.completed.status == "completed"

    after = _hash_dir(task)
    assert before == after, "scoring must not mutate the original task_dir"


def test_dual_worktree_acquired_and_released(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-run worktree is acquired from the pool and released after scoring."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    task = _make_dual_task(tmp_path / "task")

    monkeypatch.setattr(
        "codeprobe.core.executor.get_scorer",
        lambda rt: _FakeDualScorer(),
    )

    spies: list[_SpyIsolation] = []

    def _factory(repo_path: Path, namespace: str) -> IsolationStrategy:
        spy = _SpyIsolation(repo_path, namespace)
        spies.append(spy)
        return spy

    adapter = FakeAdapter()
    result = execute_task(
        adapter=adapter,
        task_dir=task,
        repo_path=repo,
        agent_config=AgentConfig(),
        reward_type="binary",
        dual_worktree_factory=_factory,
    )
    assert result.completed.status == "completed"
    assert len(spies) == 1
    spy = spies[0]
    assert len(spy.acquired) == 1
    assert len(spy.released) == 1
    assert spy.released[0] == spy.acquired[0]
    assert spy.cleaned is True


def test_no_dual_worktree_when_caller_supplies_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the caller already provides worktree_path, the executor must NOT
    create its own dual-isolation pool — that would double-isolate.
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    task = _make_dual_task(tmp_path / "task")
    caller_wt = tmp_path / "caller-worktree"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "--detach", str(caller_wt)],
        check=True,
        capture_output=True,
    )

    monkeypatch.setattr(
        "codeprobe.core.executor.get_scorer",
        lambda rt: _FakeDualScorer(),
    )

    factory_calls: list[tuple[Path, str]] = []

    def _factory(repo_path: Path, namespace: str) -> IsolationStrategy:
        factory_calls.append((repo_path, namespace))
        return _SpyIsolation(repo_path, namespace)

    adapter = FakeAdapter()
    execute_task(
        adapter=adapter,
        task_dir=task,
        repo_path=repo,
        agent_config=AgentConfig(),
        reward_type="binary",
        worktree_path=caller_wt,
        dual_worktree_factory=_factory,
    )
    assert factory_calls == []


def test_scoring_details_propagated_to_completed_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ScoreResult.details flows into CompletedTask.scoring_details as a dict."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    task = _make_dual_task(tmp_path / "task")

    monkeypatch.setattr(
        "codeprobe.core.executor.get_scorer",
        lambda rt: _FakeDualScorer(),
    )

    adapter = FakeAdapter()
    result = execute_task(
        adapter=adapter,
        task_dir=task,
        repo_path=repo,
        agent_config=AgentConfig(),
        reward_type="binary",
    )
    sd = result.completed.scoring_details
    assert isinstance(sd, dict)
    assert sd.get("score_direct") == 1.0
    assert sd.get("score_artifact") == 0.5
    assert sd.get("scoring_policy") == "weighted"
    # Backward-compatible fields preserved
    assert sd.get("passed") is True
    assert sd.get("error") is None


def test_task_scored_event_carries_scoring_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The TaskScored event populated by execute_config has scoring_details."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    task = _make_dual_task(tmp_path / "task")

    monkeypatch.setattr(
        "codeprobe.core.executor.get_scorer",
        lambda rt: _FakeDualScorer(),
    )

    listener = _CapturingListener()
    dispatcher = EventDispatcher()
    dispatcher.register(listener)

    adapter = FakeAdapter()
    cfg = ExperimentConfig(label="test", reward_type="binary")
    try:
        execute_config(
            adapter=adapter,
            task_dirs=[task],
            repo_path=repo,
            experiment_config=cfg,
            agent_config=AgentConfig(),
            event_dispatcher=dispatcher,
        )
    finally:
        dispatcher.shutdown()

    assert len(listener.events) == 1
    evt = listener.events[0]
    assert evt.scoring_details is not None
    assert evt.scoring_details.get("score_direct") == 1.0
    assert evt.scoring_details.get("score_artifact") == 0.5


def test_parallel_dual_stress_no_cross_contamination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """parallel=4 repeats=3 — 12 independent dual runs, no cross-contamination.

    Uses a synchronous stub scorer so the test can't be flaky on real I/O.
    Each run must produce its own CompletedTask with the same scoring_details
    payload (the fake scorer returns identical details on every call).
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    # Create 4 task copies; combined with repeats=3 this yields 12 runs.
    tasks = [_make_dual_task(tmp_path / f"task-{i}") for i in range(4)]

    fake = _FakeDualScorer()
    monkeypatch.setattr("codeprobe.core.executor.get_scorer", lambda rt: fake)

    adapter = FakeAdapter()
    cfg = ExperimentConfig(label="stress", reward_type="binary")
    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=repo,
        experiment_config=cfg,
        agent_config=AgentConfig(),
        parallel=4,
        repeats=3,
    )

    assert len(results) == 12
    assert len(fake.calls) == 12

    # Each scoring call must have used a unique sandbox path (TemporaryDirectory).
    unique_paths = {str(p) for p in fake.calls}
    assert len(unique_paths) == 12, "scoring sandbox dirs were re-used"

    # Every result is independently completed and carries the dual details.
    for r in results:
        assert r.status == "completed"
        assert r.scoring_details.get("score_direct") == 1.0
        assert r.scoring_details.get("score_artifact") == 0.5

    # Original task_dirs untouched.
    for t in tasks:
        assert not (t / "answer.json").exists()
        assert not (t / "answer.txt").exists()
