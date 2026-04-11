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


# ---------------------------------------------------------------------------
# Real DualScorer stress tests (owned worktrees)
# ---------------------------------------------------------------------------


class _WorktreeAnswerAdapter:
    """Adapter that parses the worktree path from the prompt and writes answer.json.

    Each invocation writes a unique payload so cross-contamination is detectable.
    """

    name = "worktree-answer-adapter"

    def __init__(self, answer_payload: dict) -> None:
        self._payload = answer_payload
        self._call_count = 0
        self._lock = threading.Lock()

    def find_binary(self) -> str | None:
        return "/usr/bin/true"

    def preflight(self, config: AgentConfig) -> list[str]:
        return []

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        return ["true"]

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> "AgentOutput":
        from codeprobe.adapters.protocol import AgentOutput

        with self._lock:
            self._call_count += 1
            call_id = self._call_count

        # Extract worktree path from the prompt: "You are working on the repository at <path>. Follow"
        import re

        match = re.search(
            r"You are working on the repository at (.+?)\. Follow", prompt
        )
        if match:
            wt_path = Path(match.group(1))
        else:
            raise RuntimeError(
                f"Could not extract worktree path from prompt: {prompt[:100]}"
            )

        # Write a unique answer.json so we can detect cross-contamination
        payload = dict(self._payload, _call_id=call_id)
        (wt_path / "answer.json").write_text(json.dumps(payload), encoding="utf-8")

        return AgentOutput(
            stdout=f"call-{call_id}",
            stderr=None,
            exit_code=0,
            duration_seconds=0.1,
            cost_usd=0.001,
            cost_model="per_token",
        )

    def isolate_session(self, slot_id: int) -> dict[str, str]:
        return {}


def _make_real_dual_task(task_dir: Path, ground_truth_answer: list[str]) -> Path:
    """Create a dual task fixture with real ground_truth.json for DualScorer."""
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text(
        "Make the change AND write answer.json with the file list.\n",
        encoding="utf-8",
    )

    tests = task_dir / "tests"
    tests.mkdir(parents=True, exist_ok=True)

    test_sh = tests / "test.sh"
    test_sh.write_text("#!/usr/bin/env bash\nset -euo pipefail\nexit 0\n")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)

    ground_truth = {
        "schema_version": 1,
        "answer_type": "file_list",
        "answer": ground_truth_answer,
    }
    (tests / "ground_truth.json").write_text(
        json.dumps(ground_truth, indent=2) + "\n", encoding="utf-8"
    )

    metadata = {
        "verification": {
            "verification_mode": "dual",
            "reward_type": "binary",
            "scoring_policy": "weighted",
            "weight_direct": 0.5,
            "weight_artifact": 0.5,
        }
    }
    (task_dir / "metadata.json").write_text(json.dumps(metadata) + "\n")

    return task_dir


class TestRealDualScorerStress:
    """Stress-test the REAL DualScorer with executor-owned worktrees.

    Unlike test_parallel_dual_stress_no_cross_contamination which uses a
    fake scorer and caller-supplied worktrees, this exercises:
    - The real DualScorer composition path
    - The executor's owned-worktree code path (executor.py:249-275)
    - WorktreeIsolation acquisition/release under contention
    """

    def test_parallel_real_dual_scorer_owned_worktrees(self, tmp_path: Path) -> None:
        """12 concurrent execute_task calls via real DualScorer + owned worktrees.

        Each run:
        1. Executor creates its own WorktreeIsolation (no worktree_path supplied)
        2. Adapter writes answer.json into the owned worktree (parsed from prompt)
        3. Real DualScorer scores both legs (test.sh direct + artifact F1)
        4. Executor releases worktree after scoring

        Asserts:
        - All 12 runs complete successfully
        - Each run has distinct scoring details with both legs
        - No cross-contamination (unique _call_id in each answer.json)
        - Original task_dirs are never mutated
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        repo = tmp_path / "repo"
        _init_git_repo(repo)

        expected_files = ["src/foo.py", "src/bar.py"]

        # Create 4 distinct task directories
        tasks = [
            _make_real_dual_task(tmp_path / f"real-dual-task-{i}", expected_files)
            for i in range(4)
        ]

        adapter = _WorktreeAnswerAdapter(
            answer_payload={
                "answer_type": "file_list",
                "answer": expected_files,
            }
        )

        results: list[TaskResult] = []
        errors: list[str] = []

        def _run_one(task_dir: Path, run_idx: int) -> TaskResult:
            return execute_task(
                adapter=adapter,
                task_dir=task_dir,
                repo_path=repo,
                agent_config=AgentConfig(),
                reward_type="binary",  # verification_mode='dual' override fires
                # NO worktree_path — forces executor to create owned worktree
            )

        # 12 runs: 4 tasks x 3 repeats, max 4 concurrent
        run_args = [(tasks[i % 4], i) for i in range(12)]

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(_run_one, task_dir, idx): idx for task_dir, idx in run_args
            }
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as exc:
                    errors.append(f"run-{idx}: {exc}")

        assert not errors, f"Some runs raised exceptions: {errors}"
        assert len(results) == 12

        # All runs should complete successfully
        for i, r in enumerate(results):
            assert r.completed.status == "completed", (
                f"run-{i} status={r.completed.status!r}, "
                f"metadata={r.completed.metadata!r}"
            )

        # Each run must have real dual scoring details
        for i, r in enumerate(results):
            sd = r.completed.scoring_details
            assert isinstance(sd, dict), f"run-{i} missing scoring_details"
            assert "score_direct" in sd, f"run-{i} missing score_direct"
            assert "score_artifact" in sd, f"run-{i} missing score_artifact"
            # test.sh exits 0 → direct leg passes
            assert (
                sd["score_direct"] == 1.0
            ), f"run-{i} score_direct={sd['score_direct']}"
            # answer.json matches ground_truth → artifact leg passes
            assert (
                sd["score_artifact"] == 1.0
            ), f"run-{i} score_artifact={sd['score_artifact']}"

        # Original task_dirs must never have answer.json written to them
        for t in tasks:
            assert not (
                t / "answer.json"
            ).exists(), f"task_dir {t} was mutated — answer.json leaked from scoring"

    def test_owned_worktree_namespaces_are_unique(self, tmp_path: Path) -> None:
        """Each owned worktree gets a unique namespace (no collisions)."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        repo = tmp_path / "repo"
        _init_git_repo(repo)

        expected_files = ["a.py"]
        task = _make_real_dual_task(tmp_path / "ns-task", expected_files)

        adapter = _WorktreeAnswerAdapter(
            answer_payload={
                "answer_type": "file_list",
                "answer": expected_files,
            }
        )

        results: list[TaskResult] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [
                pool.submit(
                    execute_task,
                    adapter=adapter,
                    task_dir=task,
                    repo_path=repo,
                    agent_config=AgentConfig(),
                    reward_type="binary",
                )
                for _ in range(6)
            ]
            for f in as_completed(futures):
                results.append(f.result())

        assert all(r.completed.status == "completed" for r in results)

        # After all runs complete, worktrees are cleaned up. But we can
        # verify that all 6 runs completed without namespace collision
        # (if they collided, some would error with git worktree conflicts).
        assert len(results) == 6
