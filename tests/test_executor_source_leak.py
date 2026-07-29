"""Regression tests for codeprobe-o8pw: source-checkout answer-leak guard.

Background
----------
An R0 K=3 comprehension campaign ran tasks in per-slot worktrees, yet the
pinned source checkouts (marshmallow, requests, gunicorn, rich) each ended up
with an untracked root ``answer.json``. In ``--uncontained`` runs the agent
process can write anywhere on disk, and a model occasionally resolves "the
repository root" to the real source checkout (the parent of its slot) rather
than the worktree it was pointed at. That stray file never reaches scoring
(which reads the slot), but it permanently dirties the checkout, making later
``--allow-dirty`` runs misleading and risking cross-seed state.

The executor now records which answer artifacts pre-exist at the checkout root
before a worktree-isolated run and sweeps anything new on every exit path
(success, agent error, timeout), without touching a caller's own file.

These tests prove the checkout root stays byte-clean and exercise the guard
directly rather than reading executor internals.
"""

from __future__ import annotations

import json
import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from codeprobe.adapters.protocol import AgentConfig, AgentOutput
from codeprobe.core.executor import execute_config, execute_task
from codeprobe.core.isolation import WorktreeIsolation
from codeprobe.core.scoring import ScoreResult
from codeprobe.models.experiment import ExperimentConfig
from tests.conftest import FakeAdapter

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _init_git_repo(repo: Path) -> None:
    """Initialise a git repo with one commit so worktrees can be added."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
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
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
    )


def _make_comprehension_task(task_dir: Path) -> Path:
    """A minimal artifact-scored task: the agent writes answer.json to root."""
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text("Write your answer to `answer.json` in the repository root.\n")
    (task_dir / "metadata.json").write_text(json.dumps({"verification": {}}))
    return task_dir


def _root_untracked(repo: Path) -> list[str]:
    """Untracked path names git reports at the checkout, worktree dirs aside."""
    out = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    names: list[str] = []
    for line in out.splitlines():
        path = line[3:]
        if path.startswith(".codeprobe"):
            continue  # slot pool + index dirs are excluded by design
        names.append(path)
    return names


class _PassScorer:
    """Trivial scorer so the success path reaches teardown deterministically."""

    def __init__(self) -> None:
        self.calls: list[Path] = []
        self._lock = threading.Lock()

    def score(self, agent_output: str, task_dir: Path) -> ScoreResult:
        with self._lock:
            self.calls.append(Path(task_dir))
        return ScoreResult(score=1.0, passed=True, details={})


class _RootLeakAdapter(FakeAdapter):
    """Agent that leaks answer artifacts to the source-checkout root.

    Reproduces an ``--uncontained`` model writing to ``repo_path`` (the parent
    of its slot) instead of, or in addition to, its worktree. ``mode`` selects
    the exit path so every teardown route is exercised.
    """

    def __init__(
        self,
        root: Path,
        *,
        mode: str = "success",
        also_write_slot: bool = True,
        leak_names: tuple[str, ...] = ("answer.json",),
        leak_content: str = '{"answer": "true"}\n',
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._root = root
        self._mode = mode
        self._also_write_slot = also_write_slot
        self._leak_names = leak_names
        self._leak_content = leak_content
        self._lock = threading.Lock()

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
        with self._lock:
            self.run_calls.append((prompt, config))
        # The leak: write to the source-checkout root the agent should never
        # touch when it is running inside an isolated worktree.
        for name in self._leak_names:
            (self._root / name).write_text(self._leak_content, encoding="utf-8")
        # Also drop the answer where scoring looks, so a success path still
        # has a valid artifact in the slot.
        if self._also_write_slot and config.cwd:
            (Path(config.cwd) / "answer.json").write_text('{"answer": "true"}\n', encoding="utf-8")
        if self._mode == "timeout":
            raise subprocess.TimeoutExpired(cmd="fake-agent", timeout=1)
        if self._mode == "failure":
            return AgentOutput(
                stdout="",
                stderr="boom",
                exit_code=1,
                duration_seconds=0.1,
                error="agent failed",
                error_category="agent",
                error_terminal=True,
            )
        return AgentOutput(stdout="done", stderr=None, exit_code=0, duration_seconds=0.1)


# ---------------------------------------------------------------------------
# execute_task — success / failure / timeout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["success", "failure", "timeout"])
def test_leaked_root_answer_swept_on_every_exit_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """A stray root answer.json is removed after success, failure, and timeout."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    task = _make_comprehension_task(tmp_path / "task")

    monkeypatch.setattr("codeprobe.core.executor.get_scorer", lambda rt: _PassScorer())

    iso = WorktreeIsolation(repo, pool_size=1, namespace="leak")
    slot = iso.acquire()
    try:
        adapter = _RootLeakAdapter(repo, mode=mode)
        result = execute_task(
            adapter=adapter,
            task_dir=task,
            repo_path=repo,
            agent_config=AgentConfig(),
            worktree_path=slot,
        )
    finally:
        iso.release(slot)
        iso.cleanup()

    # The agent DID leak — but the guard swept it on the way out.
    assert not (repo / "answer.json").exists(), f"leaked answer.json survived a {mode} run"
    assert _root_untracked(repo) == [], f"checkout root not byte-clean after {mode} run"
    # The trial still produced a result (never raised).
    assert result.completed.task_id == "task"


def test_actual_sweep_emits_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A real sweep surfaces a WARNING naming the artifact and the root.

    The sweep is the only operational signal that a model mislocated its
    answer, so a recurring leak on one task stays visible in the logs.
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    task = _make_comprehension_task(tmp_path / "task")
    monkeypatch.setattr("codeprobe.core.executor.get_scorer", lambda rt: _PassScorer())

    iso = WorktreeIsolation(repo, pool_size=1, namespace="leak")
    slot = iso.acquire()
    try:
        adapter = _RootLeakAdapter(repo)
        with caplog.at_level(logging.WARNING, logger="codeprobe.core.executor"):
            execute_task(
                adapter=adapter,
                task_dir=task,
                repo_path=repo,
                agent_config=AgentConfig(),
                worktree_path=slot,
            )
    finally:
        iso.release(slot)
        iso.cleanup()

    swept = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and "swept agent-leaked" in r.getMessage()
    ]
    assert any("answer.json" in m for m in swept), (
        f"expected a sweep WARNING naming answer.json, saw: {swept}"
    )


def test_clean_run_emits_no_sweep_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A run that leaks nothing must stay silent — no false-alarm warning."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    task = _make_comprehension_task(tmp_path / "task")
    monkeypatch.setattr("codeprobe.core.executor.get_scorer", lambda rt: _PassScorer())

    iso = WorktreeIsolation(repo, pool_size=1, namespace="leak")
    slot = iso.acquire()
    try:
        # Writes only into the slot (the correct location); no root leak.
        adapter = _RootLeakAdapter(repo, leak_names=(), also_write_slot=True)
        with caplog.at_level(logging.WARNING, logger="codeprobe.core.executor"):
            execute_task(
                adapter=adapter,
                task_dir=task,
                repo_path=repo,
                agent_config=AgentConfig(),
                worktree_path=slot,
            )
    finally:
        iso.release(slot)
        iso.cleanup()

    assert not [
        r for r in caplog.records if "swept agent-leaked" in r.getMessage()
    ], "clean run must not emit a sweep warning"


def test_leak_guard_covers_all_answer_artifact_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """answer.txt and reward.txt leak the same way and are swept too."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    task = _make_comprehension_task(tmp_path / "task")
    monkeypatch.setattr("codeprobe.core.executor.get_scorer", lambda rt: _PassScorer())

    iso = WorktreeIsolation(repo, pool_size=1, namespace="leak")
    slot = iso.acquire()
    try:
        adapter = _RootLeakAdapter(
            repo,
            leak_names=("answer.json", "answer.txt", "reward.txt"),
            also_write_slot=True,
        )
        execute_task(
            adapter=adapter,
            task_dir=task,
            repo_path=repo,
            agent_config=AgentConfig(),
            worktree_path=slot,
        )
    finally:
        iso.release(slot)
        iso.cleanup()

    for name in ("answer.json", "answer.txt", "reward.txt"):
        assert not (repo / name).exists(), f"{name} leak survived"
    assert _root_untracked(repo) == []


def test_preexisting_user_answer_file_is_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller's own root answer.json present before the run is NOT deleted."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    task = _make_comprehension_task(tmp_path / "task")
    monkeypatch.setattr("codeprobe.core.executor.get_scorer", lambda rt: _PassScorer())

    # The user's own artifact, untracked at the checkout root before the run.
    user_marker = '{"user": "keep me"}\n'
    (repo / "answer.json").write_text(user_marker, encoding="utf-8")

    iso = WorktreeIsolation(repo, pool_size=1, namespace="leak")
    slot = iso.acquire()
    try:
        # The agent leaks a DIFFERENT artifact so the pre-existing-answer.json
        # branch (preserve) and the new-artifact branch (sweep) are both hit.
        adapter = _RootLeakAdapter(repo, leak_names=("reward.txt",), also_write_slot=True)
        execute_task(
            adapter=adapter,
            task_dir=task,
            repo_path=repo,
            agent_config=AgentConfig(),
            worktree_path=slot,
        )
    finally:
        iso.release(slot)
        iso.cleanup()

    # Pre-existing user file untouched; freshly leaked artifact swept.
    assert (repo / "answer.json").read_text(encoding="utf-8") == user_marker
    assert not (repo / "reward.txt").exists()


def test_no_worktree_run_keeps_answer_in_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without worktree isolation, repo_path IS the workspace — do not sweep.

    A library caller that runs directly in repo_path legitimately writes
    answer.json there; the guard must stay off so scoring can read it.
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    task = _make_comprehension_task(tmp_path / "task")
    monkeypatch.setattr("codeprobe.core.executor.get_scorer", lambda rt: _PassScorer())

    adapter = _RootLeakAdapter(repo, also_write_slot=False)
    execute_task(
        adapter=adapter,
        task_dir=task,
        repo_path=repo,
        agent_config=AgentConfig(),
        # No worktree_path: repo_path is the effective workspace.
    )

    assert (repo / "answer.json").exists(), "answer.json in the real workspace must not be swept"


# ---------------------------------------------------------------------------
# execute_config — parallel dispatch
# ---------------------------------------------------------------------------


def test_parallel_runs_leave_checkout_byte_clean(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """parallel=4 repeats=3 — every trial leaks; the checkout stays clean."""
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    tasks = [_make_comprehension_task(tmp_path / f"task-{i}") for i in range(4)]
    monkeypatch.setattr("codeprobe.core.executor.get_scorer", lambda rt: _PassScorer())

    adapter = _RootLeakAdapter(repo)
    cfg = ExperimentConfig(label="leakstress", reward_type="binary")
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
    assert not (repo / "answer.json").exists()
    assert _root_untracked(repo) == [], "checkout root dirty after parallel run"


def test_concurrent_execute_task_calls_do_not_delete_each_others_workspaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent trials sharing repo_path sweep only the shared-root leak.

    Each trial owns a distinct slot; the guard targets repo_path (shared), so
    a stray root answer.json is removed while every slot's own answer.json
    (where scoring reads) is left intact.
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr("codeprobe.core.executor.get_scorer", lambda rt: _PassScorer())

    iso = WorktreeIsolation(repo, pool_size=4, namespace="leak")
    slots = [iso.acquire() for _ in range(4)]
    tasks = [_make_comprehension_task(tmp_path / f"task-{i}") for i in range(4)]
    adapter = _RootLeakAdapter(repo)

    # Concurrent trials sharing one repo_path must share one baseline, or a
    # late-starting trial would misread a sibling's leak as pre-existing. The
    # root is clean here, so the baseline is empty.
    baseline: frozenset[str] = frozenset()

    def _run(i: int) -> str:
        res = execute_task(
            adapter=adapter,
            task_dir=tasks[i],
            repo_path=repo,
            agent_config=AgentConfig(),
            worktree_path=slots[i],
            source_root_baseline=baseline,
        )
        return res.completed.status

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(_run, range(4)))
    finally:
        for slot in slots:
            iso.release(slot)
        iso.cleanup()

    assert all(s == "completed" for s in statuses)
    assert not (repo / "answer.json").exists()
    assert _root_untracked(repo) == []
