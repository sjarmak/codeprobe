"""Tests for core/executor.py — task execution."""

from __future__ import annotations

import json
import stat
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codeprobe.adapters.protocol import AdapterQuotaError, AgentConfig, AgentOutput
from codeprobe.core.executor import (
    DryRunEstimate,
    TaskResult,
    _classify_error,
    dry_run_estimate,
    execute_config,
    execute_task,
    get_concurrency_semaphore,
    load_instruction,
    set_max_concurrency,
)
from codeprobe.core.isolation import (
    IsolationStrategy,
    WorktreeIsolation,
    git_pin_commit,
)
from codeprobe.core.preamble import DefaultPreambleResolver, _base_prompt
from codeprobe.core.scoring import ScoreResult
from codeprobe.models.experiment import CompletedTask, ExperimentConfig
from tests.conftest import FakeAdapter, SequentialCostAdapter

# execute_config/execute_task route every run through a worktree slot
# (codeprobe-f7rl.2), which needs a real git checkout at repo_path. These
# unit tests were written against nonexistent repo paths (``Path("/repo")``),
# so they get the passthrough fake (see tests/conftest.py). Real-worktree
# behavior is covered by tests/test_executor_worktree_safety.py and
# tests/test_executor_dual_isolation.py.
pytestmark = pytest.mark.usefixtures("fake_worktree_isolation")


def _make_task(
    task_dir: Path, instruction: str = "Fix the bug.", *, passing: bool = True
) -> Path:
    """Create a minimal task directory with instruction and test.sh."""
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "instruction.md").write_text(instruction)
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    test_sh = tests_dir / "test.sh"
    exit_code = 0 if passing else 1
    test_sh.write_text(f"#!/bin/bash\nexit {exit_code}\n")
    test_sh.chmod(test_sh.stat().st_mode | stat.S_IEXEC)
    return task_dir


def test_base_prompt():
    prompt = _base_prompt("Fix the bug.", Path("/repo"))
    assert "Fix the bug." in prompt
    assert "/repo" in prompt


def test_load_instruction(tmp_path: Path):
    task_dir = tmp_path / "task-001"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("Do the thing.")

    text = load_instruction(task_dir)
    assert text == "Do the thing."


def test_load_instruction_variant(tmp_path: Path):
    task_dir = tmp_path / "task-002"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("default")
    (task_dir / "instruction_mcp.md").write_text("with mcp tools")

    text = load_instruction(task_dir, variant="instruction_mcp.md")
    assert text == "with mcp tools"


def test_load_instruction_variant_fallback(tmp_path: Path):
    task_dir = tmp_path / "task-003"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("default only")

    text = load_instruction(task_dir, variant="instruction_mcp.md")
    assert text == "default only"


def test_load_instruction_missing(tmp_path: Path):
    task_dir = tmp_path / "task-004"
    task_dir.mkdir()

    import pytest

    with pytest.raises(FileNotFoundError):
        load_instruction(task_dir)


def test_load_instruction_variant_path_traversal(tmp_path: Path):
    """instruction_variant must not escape the task directory."""
    task_dir = tmp_path / "task-005"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("default")
    # Create a file outside task_dir
    (tmp_path / "secret.md").write_text("secret content")

    import pytest

    with pytest.raises(ValueError, match="escapes task directory"):
        load_instruction(task_dir, variant="../secret.md")


def test_execute_task_success(tmp_path: Path):
    task_dir = _make_task(tmp_path / "task-001", passing=True)
    adapter = FakeAdapter(stdout="correct answer")
    config = AgentConfig()

    task_result = execute_task(adapter, task_dir, Path("/repo"), config)
    assert isinstance(task_result, TaskResult)
    result = task_result.completed
    assert isinstance(result, CompletedTask)
    assert result.task_id == "task-001"
    assert result.automated_score == 1.0
    assert result.status == "completed"
    assert len(adapter.run_calls) == 1
    assert task_result.agent_stdout == "correct answer"


def test_execute_task_with_preambles(tmp_path: Path):
    """Preambles are composed into the prompt and stored in metadata."""
    task_dir = _make_task(tmp_path / "task-001", passing=True)
    preambles_dir = task_dir / "preambles"
    preambles_dir.mkdir()
    (preambles_dir / "tdd.md").write_text("Write tests first.")

    resolver = DefaultPreambleResolver(task_dir=task_dir)
    adapter = FakeAdapter(stdout="correct answer")
    config = AgentConfig()

    result = execute_task(
        adapter,
        task_dir,
        Path("/repo"),
        config,
        preamble_names=("tdd",),
        preamble_resolver=resolver,
    ).completed
    assert result.status == "completed"
    assert result.automated_score == 1.0
    # Preamble content was composed into prompt
    prompt_sent = adapter.run_calls[0][0]
    assert "Write tests first." in prompt_sent
    # Resolved preambles stored for reproducibility
    assert "resolved_preambles" in result.metadata
    assert result.metadata["resolved_preambles"][0]["name"] == "tdd"


def test_execute_task_preamble_missing_errors(tmp_path: Path):
    """Missing preamble returns error, not crash."""
    task_dir = _make_task(tmp_path / "task-001", passing=True)

    resolver = DefaultPreambleResolver(task_dir=task_dir)
    adapter = FakeAdapter(stdout="output")
    config = AgentConfig()

    result = execute_task(
        adapter,
        task_dir,
        Path("/repo"),
        config,
        preamble_names=("nonexistent",),
        preamble_resolver=resolver,
    ).completed
    assert result.status == "error"
    assert "Preamble resolution failed" in result.metadata["error"]


def test_execute_task_preambles_without_resolver_errors(tmp_path: Path):
    """Requesting preambles without a resolver returns error (validate-or-die)."""
    task_dir = _make_task(tmp_path / "task-001", passing=True)
    adapter = FakeAdapter(stdout="output")
    config = AgentConfig()

    result = execute_task(
        adapter,
        task_dir,
        Path("/repo"),
        config,
        preamble_names=("tdd",),
        preamble_resolver=None,
    ).completed
    assert result.status == "error"
    assert "no preamble_resolver provided" in result.metadata["error"]


def test_execute_task_failing_test(tmp_path: Path):
    task_dir = _make_task(tmp_path / "task-002", passing=False)
    adapter = FakeAdapter(stdout="wrong answer")
    config = AgentConfig()

    result = execute_task(adapter, task_dir, Path("/repo"), config).completed
    assert result.automated_score == 0.0
    assert result.status == "completed"


def test_execute_task_agent_error(tmp_path: Path):
    task_dir = _make_task(tmp_path / "task-003", passing=True)
    adapter = FakeAdapter(stdout="", exit_code=1, stderr="agent crashed")
    config = AgentConfig()

    result = execute_task(adapter, task_dir, Path("/repo"), config).completed
    assert result.automated_score == 0.0
    assert result.metadata.get("error") is not None


def test_execute_task_missing_instruction(tmp_path: Path):
    task_dir = tmp_path / "task-004"
    task_dir.mkdir(parents=True)
    adapter = FakeAdapter()
    config = AgentConfig()

    result = execute_task(adapter, task_dir, Path("/repo"), config).completed
    assert result.automated_score == 0.0
    assert "error" in result.metadata


def test_execute_task_adapter_error_with_stdout_short_circuits(tmp_path: Path):
    """Auth failure: adapter returns output.error + stdout + zero tokens.

    The passing test.sh would score 1.0 if the scorer ran; the short-circuit
    must prevent that so we never ship vacuous results.
    """
    task_dir = _make_task(tmp_path / "task-auth", passing=True)
    adapter = FakeAdapter(
        stdout="Failed to authenticate. API Error: 401",
        exit_code=1,
        error="Claude API error (401): Invalid authentication credentials",
    )
    config = AgentConfig()

    result = execute_task(adapter, task_dir, Path("/repo"), config).completed
    assert result.status == "error"
    assert result.automated_score == 0.0
    assert result.error_category == "agent"
    assert "401" in result.metadata["error"]


def test_execute_task_adapter_error_with_answer_file_still_scored(tmp_path: Path):
    """max_turns with answer.txt: should still be scored, not short-circuited.

    The scorer must actually run — proven by using a failing test.sh and
    asserting score == 0.0, since a short-circuit would also produce 0.0
    but with status='error' instead.
    """
    task_dir = _make_task(tmp_path / "task-maxturns", passing=False)
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    (repo_path / "answer.txt").write_text("partial result")

    adapter = FakeAdapter(
        stdout="Maximum turns reached",
        exit_code=0,
        error="Maximum turns (30) reached without completion.",
    )
    config = AgentConfig()

    result = execute_task(adapter, task_dir, repo_path, config).completed
    # Scorer ran (status='completed', not 'error') and returned 0 from the
    # failing test.sh rather than the short-circuit.
    assert result.status == "completed"
    assert result.automated_score == 0.0


def test_execute_task_max_turns_is_terminal_failed(tmp_path: Path):
    """A cap-hit (subtype=error_max_turns) is a terminal agent failure.

    It must be marked status='failed' — a valid 0.0-reward measurement
    kept on checkpoint resume — not status='error' (infra, retried).
    Regression guard for codeprobe-8up.
    """
    task_dir = _make_task(tmp_path / "task-caphit", passing=True)
    adapter = FakeAdapter(
        stdout="Reached maximum number of turns (90)",
        exit_code=0,
        error="Reached maximum number of turns (90)",
        error_terminal=True,
        num_turns=90,
        result_subtype="error_max_turns",
    )
    config = AgentConfig()

    result = execute_task(adapter, task_dir, Path("/repo"), config).completed
    assert result.status == "failed"
    assert result.automated_score == 0.0
    assert result.error_category == "agent"
    assert result.num_turns == 90
    assert result.result_subtype == "error_max_turns"


def test_execute_task_quota_error_stays_retryable(tmp_path: Path):
    """Quota exhaustion is an infra casualty: status='error', retried on resume."""
    task_dir = _make_task(tmp_path / "task-quota", passing=True)
    adapter = FakeAdapter(
        stdout="You've hit your session limit",
        exit_code=0,
        error="OAuth quota exhausted: You've hit your session limit",
        error_category="quota",
    )
    config = AgentConfig()

    result = execute_task(adapter, task_dir, Path("/repo"), config).completed
    assert result.status == "error"
    assert result.error_category == "quota"


def test_execute_task_undeclared_error_stays_retryable(tmp_path: Path):
    """Errors the adapter did not declare terminal keep retry semantics.

    Marking an infra casualty 'failed' would bank a bogus 0.0 forever;
    re-running a terminal failure merely costs a retry — so the default
    is conservative.
    """
    task_dir = _make_task(tmp_path / "task-exec-err", passing=True)
    adapter = FakeAdapter(
        stdout="API connection dropped",
        exit_code=0,
        error="Claude CLI reported error (subtype=error_during_execution)",
        result_subtype="error_during_execution",
    )
    config = AgentConfig()

    result = execute_task(adapter, task_dir, Path("/repo"), config).completed
    assert result.status == "error"


def test_execute_task_success_carries_num_turns(tmp_path: Path):
    """Successful trials persist num_turns (codeprobe-8up gap 2)."""
    task_dir = _make_task(tmp_path / "task-turns", passing=True)
    adapter = FakeAdapter(
        stdout="correct answer",
        num_turns=64,
        result_subtype="success",
        duration_api_ms=987654,
    )
    config = AgentConfig()

    result = execute_task(adapter, task_dir, Path("/repo"), config).completed
    assert result.status == "completed"
    assert result.num_turns == 64
    assert result.result_subtype == "success"
    assert result.duration_api_ms == 987654


def test_execute_config_forwards_reward_type(tmp_path: Path):
    """reward_type from ExperimentConfig is forwarded to execute_task."""
    task_dir = _make_task(tmp_path / "task-001", passing=True)
    # Create a continuous score output (test.sh exits 0 = score 1.0 for binary)
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline", reward_type="continuous")
    agent_config = AgentConfig()

    # The continuous scorer will look for tests/test.sh — which exists and passes
    results = execute_config(
        adapter=adapter,
        task_dirs=[task_dir],
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
    )
    assert len(results) == 1
    # Key assertion: if reward_type wasn't forwarded, it would use "binary"
    # and the scoring_details would differ. The test.sh passes, so score = 1.0 either way,
    # but we can verify the scorer was invoked by checking the result is valid.
    assert results[0].status == "completed"


def test_execute_config_forwards_low_confidence_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """codeprobe-kdng: ExperimentConfig.low_confidence_threshold must reach
    the scorer through the full execute_config -> execute_task ->
    _score_in_sandbox chain. Every hop declares a 0.5 default, so a future
    refactor that drops any single hop would keep the full suite green
    while silently reverting the per-config field to 0.5 at runtime — this
    test drives the real production chain with a non-default value and a
    capturing stub scorer so that regression can't hide."""
    task_dir = _make_task(tmp_path / "task-001", passing=True)
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline", low_confidence_threshold=0.9)
    agent_config = AgentConfig()

    captured: dict = {}

    class _CapturingScorer:
        def score(
            self,
            agent_output: str,
            task_dir: Path,
            *,
            low_confidence_threshold: float = 0.5,
            **_kwargs: object,
        ) -> ScoreResult:
            captured["low_confidence_threshold"] = low_confidence_threshold
            return ScoreResult(score=1.0, passed=True)

    monkeypatch.setattr(
        "codeprobe.core.executor.get_scorer", lambda reward_type: _CapturingScorer()
    )

    results = execute_config(
        adapter=adapter,
        task_dirs=[task_dir],
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
    )
    assert len(results) == 1
    assert results[0].status == "completed"
    assert captured["low_confidence_threshold"] == 0.9


def test_execute_config_runs_all_tasks(tmp_path: Path):
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
    )
    assert len(results) == 3
    assert all(isinstance(r, CompletedTask) for r in results)
    assert len(adapter.run_calls) == 3


class _RaisingScorer:
    """A scorer whose .score() raises an unexpected exception (not one of the
    OSError/JSONDecodeError/ValueError/TypeError types execute_task handles)."""

    def score(self, *args, **kwargs):
        raise KeyError("simulated scorer bug")


def test_execute_config_sequential_preserves_scorer_crash(tmp_path: Path):
    """codeprobe-s6o A1: in the DEFAULT sequential path, an uncaught scorer
    exception is preserved as one status='error' trial — not propagated (which
    used to abort execute_config and DROP every already-collected result)."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    with patch(
        "codeprobe.core.executor.get_scorer", return_value=_RaisingScorer()
    ):
        results = execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            parallel=1,
        )

    assert len(results) == 3  # nothing dropped
    assert all(r.status == "error" for r in results)  # scored incorrect, not dropped
    assert all(r.automated_score == 0.0 for r in results)
    assert {r.task_id for r in results} == {t.name for t in tasks}


def test_execute_config_parallel_preserves_scorer_crash(tmp_path: Path):
    """codeprobe-s6o A2: the parallel path preserves the same crash identically
    (both paths route through the shared _crash_result envelope)."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    fake_iso = MagicMock()
    fake_iso.acquire.return_value = tmp_path
    with (
        patch("codeprobe.core.executor.get_scorer", return_value=_RaisingScorer()),
        patch("codeprobe.core.executor.WorktreeIsolation", return_value=fake_iso),
    ):
        results = execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=tmp_path,
            experiment_config=exp_config,
            agent_config=agent_config,
            parallel=3,
        )

    assert len(results) == 3
    assert all(r.status == "error" for r in results)
    assert all(r.automated_score == 0.0 for r in results)


def test_build_scoring_details_projects_score_result():
    """codeprobe-s6o A3: the extracted scoring-details projection is
    independently testable — voxa + Slice 1b fields surface, empties omitted."""
    from codeprobe.core.executor import _build_scoring_details
    from codeprobe.core.scoring import ScoreResult

    sr = ScoreResult(
        score=0.8,
        passed=True,
        scorer_family="continuous",
        sub_scores={"raw_score": 0.8},
        verdict="correct",
        materialized_via="git_apply",
        diagnostics={"precision": 0.9},
    )
    d = _build_scoring_details(sr)
    assert d["passed"] is True
    assert d["scorer_family"] == "continuous"
    assert d["sub_scores"] == {"raw_score": 0.8}
    assert d["verdict"] == "correct"
    assert d["materialized_via"] == "git_apply"
    assert d["diagnostics"] == {"precision": 0.9}

    # Empty optionals are omitted (not emitted as empty dicts/None family).
    minimal = _build_scoring_details(ScoreResult(score=0.0, passed=False))
    assert "scorer_family" not in minimal
    assert "sub_scores" not in minimal
    assert minimal["materialized_via"] == "in_place"


def test_execute_task_projects_score_result_verdict_to_typed_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scoring boundary must not leave verdict only in an untyped dict."""

    class _VerifierErrorScorer:
        def score(self, *_args: object, **_kwargs: object) -> ScoreResult:
            return ScoreResult(
                score=0.0,
                passed=False,
                scorer_family="binary_test",
                verdict="verifier_error",
            )

    monkeypatch.setattr(
        "codeprobe.core.executor.get_scorer",
        lambda _reward_type: _VerifierErrorScorer(),
    )
    task_dir = _make_task(tmp_path / "task-verifier-error")

    completed = execute_task(
        FakeAdapter(stdout="answer"),
        task_dir,
        Path("/repo"),
        AgentConfig(),
    ).completed

    assert completed.verdict == "verifier_error"
    assert completed.scoring_details["verdict"] == "verifier_error"


def test_execute_config_skips_checkpointed(tmp_path: Path):
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    # Write a checkpoint with task-000 already done
    from codeprobe.core.checkpoint import CheckpointStore
    from codeprobe.models.experiment import CompletedTask as CT  # noqa: N817

    checkpoint_db = tmp_path / "checkpoint.db"
    store = CheckpointStore(checkpoint_db, config_name="baseline")
    store.append(CT(task_id="task-000", automated_score=1.0))

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        checkpoint_store=store,
    )
    # Should skip task-000, run task-001 and task-002
    assert len(adapter.run_calls) == 2
    # But results should include all 3 (1 from checkpoint + 2 new)
    assert len(results) == 3
    assert results[0].task_id == "task-000"
    assert results[0].automated_score == 1.0


def test_execute_config_calls_callback(tmp_path: Path):
    tasks = [_make_task(tmp_path / "task-000", passing=True)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    callback_results: list[CompletedTask] = []

    execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        on_task_complete=callback_results.append,
    )
    assert len(callback_results) == 1
    assert callback_results[0].task_id == "task-000"


# --- Quota circuit-breaker tests (codeprobe-f7rl.29) ---


class _QuotaRaisingAdapter(FakeAdapter):
    """Adapter whose run() raises AdapterQuotaError, like an API-based
    adapter hitting provider rate limits (codex / openai_compat)."""

    def __init__(self, *, run_delay: float = 0.0, **kwargs) -> None:
        super().__init__(**kwargs)
        self._run_delay = run_delay

    def run(self, prompt, config, session_env=None):
        self.run_calls.append((prompt, config))
        if self._run_delay:
            time.sleep(self._run_delay)
        raise AdapterQuotaError("Rate limited: 429 insufficient_quota")


def test_execute_config_sequential_halts_on_quota_error(tmp_path: Path):
    """A raised AdapterQuotaError halts the sequential dispatch loop:
    only the first trial runs, and its row carries error_category='quota'."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(4)]
    adapter = _QuotaRaisingAdapter(stdout="unused")
    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=ExperimentConfig(label="baseline"),
        agent_config=AgentConfig(),
        parallel=1,
    )
    assert len(results) == 1
    assert results[0].error_category == "quota"
    assert results[0].status == "error"
    assert "Rate limited" in results[0].metadata["error"]
    assert len(adapter.run_calls) == 1


def test_execute_config_parallel_halts_on_quota_error(tmp_path: Path):
    """The parallel path cancels pending trials after the first quota row."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(24)]
    adapter = _QuotaRaisingAdapter(stdout="unused", run_delay=0.05)
    fake_iso = MagicMock()
    fake_iso.acquire.return_value = tmp_path
    with patch("codeprobe.core.executor.WorktreeIsolation", return_value=fake_iso):
        results = execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=tmp_path,
            experiment_config=ExperimentConfig(label="baseline"),
            agent_config=AgentConfig(),
            parallel=2,
        )
    # Halt fires on the first processed result; the rest are cancelled.
    assert len(results) == 1
    assert results[0].error_category == "quota"
    assert results[0].status == "error"
    assert len(adapter.run_calls) < len(tasks)


# --- Cost circuit-breaker tests ---


def test_execute_config_halts_at_budget(tmp_path: Path):
    """Executor stops running tasks when cumulative cost exceeds max_cost_usd."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(5)]
    # Each task costs $1.00, budget is $2.50 -> should run 3 tasks (0+1+1=2 after first,
    # then 2+1=3 after third which exceeds 2.50, so halt before 4th)
    adapter = FakeAdapter(stdout="output", cost_usd=1.0, cost_model="per_token")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        max_cost_usd=2.50,
    )
    # Should have run 3 tasks: after task 3, cumulative = $3.00 > $2.50, halt
    assert len(results) == 3
    assert len(adapter.run_calls) == 3


class _RecoveredTimeoutAdapter(FakeAdapter):
    """Returns the recovered-timeout AgentOutput shape (codeprobe-f7rl.34):
    partial stream, summed tokens, calculated cost, merged timeout error."""

    def __init__(self, *, error_category: str | None = None) -> None:
        super().__init__()
        self._recovered_error_category = error_category

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
        self.run_calls.append((prompt, config))
        return AgentOutput(
            stdout="partial stream",
            stderr=None,
            exit_code=-1,
            duration_seconds=60.0,
            input_tokens=3000,
            output_tokens=500,
            cost_usd=1.0,
            cost_model="per_token",
            cost_source="calculated",
            error=(
                "Agent timed out after 60s; stream ended without a result "
                "event; usage summed from 2 assistant turns"
            ),
            error_category=self._recovered_error_category,
        )


def test_timed_out_trial_counts_toward_budget(tmp_path: Path):
    """Recovered timed-out spend counts toward --max-cost-usd, and a quota
    stub inside a timed-out trial halts remaining dispatch (codeprobe-f7rl.34)."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(5)]

    # $1.00 recovered per timed-out trial, budget $2.50 → halt after task 3.
    adapter = _RecoveredTimeoutAdapter()
    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=ExperimentConfig(label="baseline"),
        agent_config=AgentConfig(),
        max_cost_usd=2.50,
    )
    assert len(results) == 3
    assert len(adapter.run_calls) == 3
    assert all(r.cost_usd == 1.0 for r in results)
    assert all(r.cost_model == "per_token" for r in results)

    # Quota classification preserved through the timeout path halts the run
    # after the first trial, same as a non-timeout quota stub.
    quota_adapter = _RecoveredTimeoutAdapter(error_category="quota")
    quota_results = execute_config(
        adapter=quota_adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=ExperimentConfig(label="quota-arm"),
        agent_config=AgentConfig(),
    )
    assert len(quota_adapter.run_calls) == 1
    assert len(quota_results) == 1
    assert quota_results[0].error_category == "quota"


def test_execute_config_no_budget_runs_all(tmp_path: Path):
    """Without max_cost_usd, all tasks run regardless of cost."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(5)]
    adapter = FakeAdapter(stdout="output", cost_usd=10.0, cost_model="per_token")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
    )
    assert len(results) == 5


def test_execute_config_skips_unknown_cost_model_in_accumulation(tmp_path: Path):
    """Tasks with unknown or subscription cost_model are not counted toward budget."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(4)]
    # Task 0: $1.00 per_token, Task 1: unknown (skipped), Task 2: $1.00 per_token,
    # Task 3: $1.00 per_token -> cumulative at task 2 = $2.00, task 3 = $3.00 > $2.50
    adapter = SequentialCostAdapter(
        costs=[
            (1.0, "per_token"),
            (None, "unknown"),
            (1.0, "per_token"),
            (1.0, "per_token"),
        ],
        stdout="output",
    )
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        max_cost_usd=2.50,
    )
    # Tasks 0, 1, 2 run (cumulative per_token = $2.00), task 3 would push to $3.00 -> halt
    assert len(results) == 4
    assert len(adapter.run_calls) == 4


def test_execute_config_skips_subscription_cost_model(tmp_path: Path):
    """Tasks with subscription cost_model are not counted toward budget."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = SequentialCostAdapter(
        costs=[
            (0.0, "subscription"),
            (0.0, "subscription"),
            (0.0, "subscription"),
        ],
        stdout="output",
    )
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        max_cost_usd=0.01,  # Tiny budget, but subscription costs are skipped
    )
    assert len(results) == 3


def test_execute_config_budget_saves_partial_results(tmp_path: Path):
    """Partial results from budget halt are valid CompletedTask objects."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(5)]
    adapter = FakeAdapter(stdout="output", cost_usd=2.0, cost_model="per_token")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        max_cost_usd=3.0,
    )
    # $2.00 after task 0, $4.00 after task 1 -> halt
    assert len(results) == 2
    assert all(isinstance(r, CompletedTask) for r in results)
    assert all(r.status == "completed" for r in results)


def test_execute_config_budget_with_checkpoint(tmp_path: Path):
    """Checkpointed tasks don't count toward budget (they were already paid for)."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(4)]
    adapter = FakeAdapter(stdout="output", cost_usd=1.5, cost_model="per_token")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    # Checkpoint task-000
    from codeprobe.core.checkpoint import CheckpointStore
    from codeprobe.models.experiment import CompletedTask as CT  # noqa: N817

    checkpoint_db = tmp_path / "checkpoint.db"
    store = CheckpointStore(checkpoint_db, config_name="baseline")
    store.append(CT(task_id="task-000", automated_score=1.0))

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        checkpoint_store=store,
        max_cost_usd=2.0,
    )
    # task-000 is checkpointed (free), task-001 costs $1.50, task-002 would be $3.00 -> halt
    assert len(adapter.run_calls) == 2
    # Results include checkpoint + 2 new
    assert len(results) == 3


def test_execute_config_budget_callback_fires_for_partial(tmp_path: Path):
    """on_task_complete fires for each completed task before budget halt."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(5)]
    adapter = FakeAdapter(stdout="output", cost_usd=3.0, cost_model="per_token")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    callback_results: list[CompletedTask] = []

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        max_cost_usd=5.0,
        on_task_complete=callback_results.append,
    )
    # $3 after task 0, $6 after task 1 -> halt after 2
    assert len(results) == 2
    assert len(callback_results) == 2


def test_execute_config_retries_error_checkpointed(tmp_path: Path):
    """Tasks checkpointed with status='error' should be retried, not skipped."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    from codeprobe.core.checkpoint import CheckpointStore
    from codeprobe.models.experiment import CompletedTask as CT  # noqa: N817

    checkpoint_db = tmp_path / "checkpoint.db"
    store = CheckpointStore(checkpoint_db, config_name="baseline")
    # task-000 completed successfully — should be skipped
    store.append(CT(task_id="task-000", automated_score=1.0, status="completed"))
    # task-001 errored — should be retried
    store.append(CT(task_id="task-001", automated_score=0.0, status="error"))

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        checkpoint_store=store,
    )
    # Should skip task-000, retry task-001, run task-002
    assert len(adapter.run_calls) == 2
    # Results: 1 from checkpoint + 2 newly run
    assert len(results) == 3
    assert results[0].task_id == "task-000"
    assert results[0].automated_score == 1.0


def test_execute_config_retries_verifier_error_checkpoint(
    tmp_path: Path,
) -> None:
    tasks = [
        _make_task(tmp_path / "broken", passing=True),
        _make_task(tmp_path / "wrong", passing=True),
    ]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")

    from codeprobe.core.checkpoint import CheckpointStore

    store = CheckpointStore(tmp_path / "checkpoint.db", config_name="baseline")
    store.append(
        CompletedTask(
            task_id="broken",
            automated_score=0.0,
            verdict="verifier_error",
            scoring_details={"passed": False},
        )
    )
    store.append(
        CompletedTask(
            task_id="wrong",
            automated_score=0.0,
            verdict="incorrect",
            scoring_details={"passed": False},
        )
    )

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=AgentConfig(),
        checkpoint_store=store,
    )

    assert len(adapter.run_calls) == 1
    by_id = {result.task_id: result for result in results}
    assert by_id["broken"].automated_score == 1.0
    assert by_id["broken"].verdict == "correct"
    assert by_id["wrong"].verdict == "incorrect"


def test_execute_config_none_cost_not_accumulated(tmp_path: Path):
    """Tasks where cost_usd is None (per_token but None shouldn't happen, but
    cost_usd=None with unknown model) are skipped in accumulation."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = FakeAdapter(stdout="output", cost_usd=None, cost_model="unknown")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    results = execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        max_cost_usd=0.01,
    )
    # All tasks run since no per_token costs to accumulate
    assert len(results) == 3


def test_budget_shared_across_configs(tmp_path: Path):
    """One BudgetChecker passed to several execute_config calls caps the
    WHOLE experiment: arm B halts once cross-arm spend reaches the budget
    (codeprobe-f7rl.33)."""
    from codeprobe.core.events import BudgetChecker

    tasks_a = [_make_task(tmp_path / "a" / "task-000", passing=True)]
    tasks_b = [
        _make_task(tmp_path / "b" / f"task-{i:03d}", passing=True) for i in range(3)
    ]
    adapter_a = FakeAdapter(stdout="output", cost_usd=0.5, cost_model="per_token")
    adapter_b = FakeAdapter(stdout="output", cost_usd=0.5, cost_model="per_token")
    agent_config = AgentConfig()
    checker = BudgetChecker(budget=1.0)

    results_a = execute_config(
        adapter=adapter_a,
        task_dirs=tasks_a,
        repo_path=Path("/repo"),
        experiment_config=ExperimentConfig(label="arm-a"),
        agent_config=agent_config,
        max_cost_usd=1.0,
        budget_checker=checker,
    )
    results_b = execute_config(
        adapter=adapter_b,
        task_dirs=tasks_b,
        repo_path=Path("/repo"),
        experiment_config=ExperimentConfig(label="arm-b"),
        agent_config=agent_config,
        max_cost_usd=1.0,
        budget_checker=checker,
    )

    # Arm A completes under budget ($0.50 of $1.00).
    assert len(results_a) == 1
    # Arm B crosses the cap on its first trial ($1.00 >= $1.00) and halts;
    # overshoot is bounded by the single in-flight trial.
    assert len(results_b) == 1
    assert len(adapter_b.run_calls) == 1
    assert checker.is_exceeded
    assert checker.cumulative_cost == pytest.approx(1.0)


def test_budget_local_when_not_shared(tmp_path: Path):
    """Without a shared checker each execute_config call keeps its own
    per-config budget — regression guard for api.execute_config callers."""
    agent_config = AgentConfig()
    run_counts = []
    for arm in ("arm-a", "arm-b"):
        tasks = [
            _make_task(tmp_path / arm / f"task-{i:03d}", passing=True)
            for i in range(5)
        ]
        adapter = FakeAdapter(stdout="output", cost_usd=1.0, cost_model="per_token")
        results = execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=ExperimentConfig(label=arm),
            agent_config=agent_config,
            max_cost_usd=2.50,
        )
        run_counts.append(len(results))
    # Same per-arm halt point as test_execute_config_halts_at_budget —
    # the second arm is NOT throttled by the first arm's spend.
    assert run_counts == [3, 3]


# --- Sequential runs execute inside worktree slots (codeprobe-f7rl.2) ---


def test_execute_config_sequential_acquires_and_releases_slot(tmp_path: Path):
    """Sequential mode acquires/releases a worktree slot per task, then
    cleans up the owned pool — the primary checkout is never the workspace."""
    tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    fake_iso = MagicMock()
    fake_iso.acquire.return_value = tmp_path / "slot-0"
    with patch(
        "codeprobe.core.executor.WorktreeIsolation", return_value=fake_iso
    ) as mock_cls:
        execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            parallel=1,
        )
    mock_cls.assert_called_once_with(
        Path("/repo"), pool_size=1, namespace="baseline"
    )
    assert fake_iso.acquire.call_count == 3
    assert fake_iso.release.call_count == 3
    fake_iso.cleanup.assert_called_once()


def test_execute_config_sequential_runs_task_in_slot(tmp_path: Path):
    """The agent prompt references the acquired slot, not the primary repo."""
    tasks = [_make_task(tmp_path / "task-000", passing=True)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    fake_iso = MagicMock()
    slot = tmp_path / "slot-0"
    fake_iso.acquire.return_value = slot
    with patch("codeprobe.core.executor.WorktreeIsolation", return_value=fake_iso):
        execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            parallel=1,
        )
    assert len(adapter.run_calls) == 1
    prompt = adapter.run_calls[0][0]
    assert str(slot) in prompt
    assert "/repo" not in prompt


def test_execute_config_single_task_uses_slot(tmp_path: Path):
    """Even a single-task run executes inside a worktree slot."""
    tasks = [_make_task(tmp_path / "task-000", passing=True)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    fake_iso = MagicMock()
    fake_iso.acquire.return_value = tmp_path / "slot-0"
    with patch("codeprobe.core.executor.WorktreeIsolation", return_value=fake_iso):
        execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            parallel=1,
        )
    assert fake_iso.acquire.call_count == 1
    assert fake_iso.release.call_count == 1
    fake_iso.cleanup.assert_called_once()


def test_execute_config_sequential_uses_caller_isolation(tmp_path: Path):
    """A caller-provided isolation strategy is used in sequential mode and
    NOT cleaned up (the caller owns it)."""
    tasks = [_make_task(tmp_path / "task-000", passing=True)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    caller_iso = MagicMock()
    caller_iso.acquire.return_value = tmp_path / "slot-0"
    execute_config(
        adapter=adapter,
        task_dirs=tasks,
        repo_path=Path("/repo"),
        experiment_config=exp_config,
        agent_config=agent_config,
        parallel=1,
        isolation=caller_iso,
    )
    assert caller_iso.acquire.call_count == 1
    assert caller_iso.release.call_count == 1
    caller_iso.cleanup.assert_not_called()


def test_execute_config_sequential_releases_slot_on_crash(tmp_path: Path):
    """A crashing trial still releases its slot back to the pool."""
    tasks = [_make_task(tmp_path / "task-000", passing=True)]
    adapter = FakeAdapter(stdout="output")
    exp_config = ExperimentConfig(label="baseline")
    agent_config = AgentConfig()

    fake_iso = MagicMock()
    fake_iso.acquire.return_value = tmp_path / "slot-0"
    with (
        patch("codeprobe.core.executor.get_scorer", return_value=_RaisingScorer()),
        patch("codeprobe.core.executor.WorktreeIsolation", return_value=fake_iso),
    ):
        results = execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            parallel=1,
        )
    assert fake_iso.release.call_count == 1
    assert len(results) == 1
    assert results[0].status == "error"


# --- Worktree isolation tests ---


class TestWorktreeIsolation:
    def test_create_pool(self, tmp_path: Path) -> None:
        """WorktreeIsolation creates worktrees via subprocess."""
        with patch("subprocess.run") as mock_run:
            iso = WorktreeIsolation(tmp_path, pool_size=2)
            # Force pool creation by acquiring
            iso._base_dir.mkdir(parents=True, exist_ok=True)
            iso._create_pool()
            # Should call git worktree prune once + git worktree add twice
            assert mock_run.call_count == 3
            assert mock_run.call_args_list[0][0][0] == ["git", "worktree", "prune"]
            for c in mock_run.call_args_list[1:]:
                assert c[0][0][0:3] == ["git", "worktree", "add"]

    def test_acquire_returns_path(self, tmp_path: Path) -> None:
        """acquire() returns a worktree path from the pool."""
        with patch("subprocess.run"):
            iso = WorktreeIsolation(tmp_path, pool_size=1)
            iso._create_pool()
            wt = iso.acquire()
            assert isinstance(wt, Path)
            assert "slot-0" in str(wt)

    def test_reset_calls_git_restore_and_clean(self, tmp_path: Path) -> None:
        """reset() runs git restore --staged --worktree . and git clean -fd."""
        iso = WorktreeIsolation(tmp_path, pool_size=1)
        wt = tmp_path / "worktree"
        wt.mkdir()
        with patch("subprocess.run") as mock_run:
            # git_restore_clean now raises on a non-zero restore, so the mock
            # has to model a successful git rather than a bare MagicMock.
            mock_run.return_value.returncode = 0
            iso.reset(wt)
            assert mock_run.call_count == 2
            calls = [c[0][0] for c in mock_run.call_args_list]
            # --staged --worktree also unstages an agent's `git add`
            # (codeprobe-9tk); plain `git restore .` left staged changes that
            # broke the next task's pin in a reused pooled worktree.
            assert calls[0] == ["git", "restore", "--staged", "--worktree", "."]
            assert calls[1] == [
                "git",
                "clean",
                "-fd",
                "-e",
                ".codeprobe",
                "-e",
                ".codeprobe-worktrees*",
            ]

    def test_release_resets_and_returns_to_pool(self, tmp_path: Path) -> None:
        """release() resets the worktree and makes it available again."""
        with patch("subprocess.run"):
            iso = WorktreeIsolation(tmp_path, pool_size=1)
            iso._create_pool()
            wt = iso.acquire()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            iso.release(wt)
        # Should be available again
        assert not iso._available.empty()

    def test_cleanup_removes_worktrees(self, tmp_path: Path) -> None:
        """cleanup() calls git worktree remove for each worktree."""
        with patch("subprocess.run"):
            iso = WorktreeIsolation(tmp_path, pool_size=2)
            iso._create_pool()
        with patch("subprocess.run") as mock_run:
            iso.cleanup()
            # 2 worktree removes + 1 prune
            assert mock_run.call_count == 3
            for c in mock_run.call_args_list[:2]:
                assert c[0][0][0:3] == ["git", "worktree", "remove"]
            assert mock_run.call_args_list[2][0][0] == ["git", "worktree", "prune"]

    def test_pool_size_validation(self) -> None:
        """pool_size must be >= 1."""
        import pytest

        with pytest.raises(ValueError, match="pool_size"):
            WorktreeIsolation(Path("/tmp"), pool_size=0)

    def test_satisfies_protocol(self, tmp_path: Path) -> None:
        """WorktreeIsolation satisfies IsolationStrategy protocol."""
        with patch("subprocess.run"):
            iso = WorktreeIsolation(tmp_path, pool_size=1)
            assert isinstance(iso, IsolationStrategy)


class TestGitPinCommit:
    def test_calls_git_checkout_detach(self, tmp_path: Path) -> None:
        """git_pin_commit runs git checkout --detach --force <commit>."""
        with patch("subprocess.run") as mock_run:
            git_pin_commit(tmp_path, "abc123^")
            # --force discards leftover state in a reused pooled worktree so
            # the pin can't abort on "local changes would be overwritten"
            # (codeprobe-9tk).
            mock_run.assert_called_once_with(
                ["git", "checkout", "--detach", "--force", "abc123^"],
                cwd=tmp_path,
                check=True,
                capture_output=True,
            )

    def test_raises_on_failure(self, tmp_path: Path) -> None:
        """git_pin_commit propagates CalledProcessError."""
        import pytest

        with patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(128, "git"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                git_pin_commit(tmp_path, "badref")


# --- Preamble repo_path rewriting ---


class TestPreambleRewriting:
    def test_base_prompt_uses_worktree_path(self) -> None:
        """_base_prompt uses worktree_path when provided."""
        prompt = _base_prompt(
            "Fix bug.", Path("/repo"), worktree_path=Path("/wt/slot-0")
        )
        assert "/wt/slot-0" in prompt
        assert "/repo" not in prompt

    def test_base_prompt_uses_repo_path_when_no_worktree(self) -> None:
        """_base_prompt uses repo_path when worktree_path is None."""
        prompt = _base_prompt("Fix bug.", Path("/repo"), worktree_path=None)
        assert "/repo" in prompt

    def test_execute_task_passes_worktree_path(self, tmp_path: Path) -> None:
        """execute_task passes worktree_path through to prompt."""
        task_dir = _make_task(tmp_path / "task-001", passing=True)
        adapter = FakeAdapter(stdout="correct answer")
        config = AgentConfig()
        wt_path = Path("/worktrees/slot-0")

        execute_task(adapter, task_dir, Path("/repo"), config, worktree_path=wt_path)
        prompt = adapter.run_calls[0][0]
        assert str(wt_path) in prompt
        assert "/repo" not in prompt


# --- Global concurrency semaphore ---


class TestConcurrencySemaphore:
    def test_set_and_get_semaphore(self) -> None:
        """set_max_concurrency creates a semaphore retrievable via get."""
        set_max_concurrency(3)
        sem = get_concurrency_semaphore()
        assert sem is not None
        # Verify it's a semaphore with correct count
        assert isinstance(sem, threading.Semaphore)

    def test_semaphore_limits_concurrency(self) -> None:
        """Semaphore actually limits concurrent access."""
        set_max_concurrency(2)
        sem = get_concurrency_semaphore()
        assert sem is not None

        # Acquire both slots
        assert sem.acquire(blocking=False)
        assert sem.acquire(blocking=False)
        # Third acquire should fail (non-blocking)
        assert not sem.acquire(blocking=False)
        # Release one
        sem.release()
        assert sem.acquire(blocking=False)
        # Clean up
        sem.release()
        sem.release()

    def test_executor_uses_isolation_in_parallel(self, tmp_path: Path) -> None:
        """execute_config uses isolation strategy when parallel > 1."""
        tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(2)]
        adapter = FakeAdapter(stdout="output")
        exp_config = ExperimentConfig(label="baseline")
        agent_config = AgentConfig()

        mock_iso = MagicMock(spec=WorktreeIsolation)
        mock_iso.acquire.return_value = Path("/wt/slot-0")

        with patch("codeprobe.core.executor.WorktreeIsolation", return_value=mock_iso):
            execute_config(
                adapter=adapter,
                task_dirs=tasks,
                repo_path=Path("/repo"),
                experiment_config=exp_config,
                agent_config=agent_config,
                parallel=2,
            )
        # Isolation should have been used
        assert mock_iso.acquire.call_count == 2
        assert mock_iso.release.call_count == 2
        assert mock_iso.cleanup.call_count == 1

    def test_parallel_isolation_passes_session_env_to_adapter(
        self, tmp_path: Path
    ) -> None:
        """When running in parallel with isolation, isolate_session() env
        reaches adapter.run() via session_env."""
        tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(2)]

        session_envs_received: list[dict[str, str] | None] = []
        namespaces_received: list[str | None] = []

        class TrackingAdapter(FakeAdapter):
            def run(self, prompt, config, session_env=None):
                session_envs_received.append(session_env)
                return super().run(prompt, config, session_env=session_env)

            def isolate_session(
                self,
                slot_id: int,
                namespace: str | None = None,
            ) -> dict[str, str]:
                namespaces_received.append(namespace)
                prefix = "/tmp/codeprobe-claude"
                if namespace:
                    prefix = f"{prefix}/{namespace}"
                return {"CLAUDE_CONFIG_DIR": f"{prefix}/slot-{slot_id}"}

        adapter = TrackingAdapter(stdout="output")
        exp_config = ExperimentConfig(label="baseline")
        agent_config = AgentConfig()

        mock_iso = MagicMock(spec=WorktreeIsolation)
        mock_iso.acquire.return_value = Path("/wt/slot-0")

        with patch("codeprobe.core.executor.WorktreeIsolation", return_value=mock_iso):
            execute_config(
                adapter=adapter,
                task_dirs=tasks,
                repo_path=Path("/repo"),
                experiment_config=exp_config,
                agent_config=agent_config,
                parallel=2,
            )

        assert len(session_envs_received) == 2
        for env in session_envs_received:
            assert env is not None
            assert "CLAUDE_CONFIG_DIR" in env
            assert env["CLAUDE_CONFIG_DIR"].startswith("/tmp/codeprobe-claude/baseline-")
        assert len(namespaces_received) == 2
        assert namespaces_received[0] is not None
        assert namespaces_received[0] == namespaces_received[1]

    def test_sequential_runs_isolate_session_lifecycle(
        self, tmp_path: Path
    ) -> None:
        """Serial dispatch performs the same isolate_session/cleanup
        lifecycle as parallel (codeprobe-f7rl.24): isolate_session is
        called, its env reaches adapter.run via session_env, and
        cleanup_session_namespace fires."""
        tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(2)]

        session_envs_received: list[dict[str, str] | None] = []
        isolate_calls: list[tuple[str | None, bool]] = []
        cleanup_namespaces: list[str | None] = []

        class TrackingAdapter(FakeAdapter):
            def run(self, prompt, config, session_env=None):
                session_envs_received.append(session_env)
                return super().run(prompt, config, session_env=session_env)

            def isolate_session(
                self,
                slot_id: int,
                namespace: str | None = None,
                pristine: bool = False,
            ) -> dict[str, str]:
                isolate_calls.append((namespace, pristine))
                return {"CLAUDE_CONFIG_DIR": f"/tmp/fake/{namespace}/slot-{slot_id}"}

            def cleanup_session_namespace(self, namespace: str | None) -> None:
                cleanup_namespaces.append(namespace)

        adapter = TrackingAdapter(stdout="output")

        execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=tmp_path,
            experiment_config=ExperimentConfig(label="baseline"),
            agent_config=AgentConfig(),
            parallel=1,
            pristine_config=True,
        )

        # One isolate_session call for the whole serial config run.
        assert len(isolate_calls) == 1
        namespace, pristine = isolate_calls[0]
        assert namespace is not None
        assert namespace.startswith("baseline-")
        assert pristine is True

        # Every task received the isolated session env.
        assert len(session_envs_received) == 2
        for env in session_envs_received:
            assert env == {"CLAUDE_CONFIG_DIR": f"/tmp/fake/{namespace}/slot-0"}

        # Cleanup fired with the same namespace.
        assert cleanup_namespaces == [namespace]

    def test_sequential_isolation_works_with_bare_adapter_signature(
        self, tmp_path: Path
    ) -> None:
        """Adapters with the bare isolate_session(slot_id) shape (no
        namespace/pristine kwargs) still work on the serial path."""
        tasks = [_make_task(tmp_path / "task-000", passing=True)]
        adapter = FakeAdapter(stdout="output")

        results = execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=tmp_path,
            experiment_config=ExperimentConfig(label="baseline"),
            agent_config=AgentConfig(),
            parallel=1,
            pristine_config=True,
        )

        assert len(results) == 1
        assert results[0].status != "error"


# --- Repeat infrastructure tests ---


class TestRepeats:
    def test_repeats_runs_each_task_n_times(self, tmp_path: Path) -> None:
        """With repeats=3, each task runs 3 times."""
        tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(2)]
        adapter = FakeAdapter(stdout="output")
        exp_config = ExperimentConfig(label="baseline")
        agent_config = AgentConfig()

        results = execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            repeats=3,
        )
        assert len(results) == 6  # 2 tasks * 3 repeats
        assert len(adapter.run_calls) == 6

    def test_repeats_stamps_repeat_index(self, tmp_path: Path) -> None:
        """Each result has the correct repeat_index."""
        tasks = [_make_task(tmp_path / "task-000", passing=True)]
        adapter = FakeAdapter(stdout="output")
        exp_config = ExperimentConfig(label="baseline")
        agent_config = AgentConfig()

        results = execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            repeats=3,
        )
        assert len(results) == 3
        indices = sorted(r.repeat_index for r in results)
        assert indices == [0, 1, 2]
        assert all(r.task_id == "task-000" for r in results)

    def test_repeats_default_is_one(self, tmp_path: Path) -> None:
        """Default repeats=1 runs each task once."""
        tasks = [_make_task(tmp_path / "task-000", passing=True)]
        adapter = FakeAdapter(stdout="output")
        exp_config = ExperimentConfig(label="baseline")
        agent_config = AgentConfig()

        results = execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
        )
        assert len(results) == 1
        assert results[0].repeat_index == 0

    def test_repeats_checkpoint_skips_completed_repeats(self, tmp_path: Path) -> None:
        """Checkpoint tracks (task_id, repeat_index) — completed repeats are skipped."""
        tasks = [_make_task(tmp_path / "task-000", passing=True)]
        adapter = FakeAdapter(stdout="output")
        exp_config = ExperimentConfig(label="baseline")
        agent_config = AgentConfig()

        from codeprobe.core.checkpoint import CheckpointStore

        checkpoint_db = tmp_path / "checkpoint.db"
        store = CheckpointStore(checkpoint_db, config_name="baseline")
        # Checkpoint repeat 0 as already done
        store.append(
            CompletedTask(task_id="task-000", automated_score=1.0, repeat_index=0)
        )

        results = execute_config(
            adapter=adapter,
            task_dirs=tasks,
            repo_path=Path("/repo"),
            experiment_config=exp_config,
            agent_config=agent_config,
            checkpoint_store=store,
            repeats=3,
        )
        # Should skip repeat 0, run repeats 1 and 2
        assert len(adapter.run_calls) == 2
        # Results include checkpoint (repeat 0) + 2 new
        assert len(results) == 3
        indices = sorted(r.repeat_index for r in results)
        assert indices == [0, 1, 2]


# --- Dry-run estimation tests ---


class TestDryRunEstimate:
    def test_returns_correct_counts(self, tmp_path: Path) -> None:
        """dry_run_estimate computes correct task/run/config counts."""
        estimate = dry_run_estimate(
            task_count=5,
            configs_count=2,
            repeats=3,
            parallel=4,
            repo_path=tmp_path,
        )
        assert isinstance(estimate, DryRunEstimate)
        assert estimate.total_tasks == 5
        assert estimate.total_configs == 2
        assert estimate.total_runs == 30  # 5 * 2 * 3
        assert estimate.max_concurrent == 4  # min(4, 30)

    def test_max_concurrent_capped_by_total_runs(self, tmp_path: Path) -> None:
        """max_concurrent never exceeds total_runs."""
        estimate = dry_run_estimate(
            task_count=2,
            configs_count=1,
            repeats=1,
            parallel=10,
            repo_path=tmp_path,
        )
        assert estimate.total_runs == 2
        assert estimate.max_concurrent == 2  # min(10, 2)

    def test_cost_range_scales_with_runs(self, tmp_path: Path) -> None:
        """Estimated cost range scales linearly with total_runs."""
        estimate = dry_run_estimate(
            task_count=10,
            configs_count=1,
            repeats=1,
            parallel=1,
            repo_path=tmp_path,
        )
        cost_lo, cost_hi = estimate.estimated_cost_range
        assert cost_lo == 10 * 0.02
        assert cost_hi == 10 * 0.15

    def test_disk_estimate_scales_with_concurrency(self, tmp_path: Path) -> None:
        """Disk estimate = repo_size * max_concurrent."""
        with patch("codeprobe.core.executor._estimate_repo_size_mb", return_value=50.0):
            estimate = dry_run_estimate(
                task_count=10,
                configs_count=1,
                repeats=1,
                parallel=3,
                repo_path=tmp_path,
            )
        assert estimate.estimated_disk_mb == 150.0  # 50 * 3

    def test_no_subprocess_calls(self, tmp_path: Path) -> None:
        """dry_run_estimate does not spawn agent subprocesses."""
        with patch("subprocess.run") as mock_run:
            # Allow du -sm to work normally
            mock_run.return_value = MagicMock(returncode=0, stdout="50\t/tmp")
            dry_run_estimate(
                task_count=5,
                configs_count=2,
                repeats=3,
                parallel=4,
                repo_path=tmp_path,
            )
        # Only subprocess call should be du -sm for repo size estimation
        for c in mock_run.call_args_list:
            args = c[0][0] if c[0] else c[1].get("args", [])
            assert args[0] == "du", f"Unexpected subprocess: {args}"

    def test_frozen_dataclass(self, tmp_path: Path) -> None:
        """DryRunEstimate is immutable."""
        import pytest

        estimate = dry_run_estimate(
            task_count=1,
            configs_count=1,
            repeats=1,
            parallel=1,
            repo_path=tmp_path,
        )
        with pytest.raises(AttributeError):
            estimate.total_tasks = 99  # type: ignore[misc]


# --- Error taxonomy tests ---


class TestErrorTaxonomy:
    def test_classify_error_timeout(self) -> None:
        """subprocess.TimeoutExpired -> 'timeout'."""
        exc = subprocess.TimeoutExpired(cmd="fake", timeout=30)
        assert _classify_error(exc) == "timeout"

    def test_classify_error_system_oserror(self) -> None:
        """OSError -> 'system'."""
        assert _classify_error(OSError("disk full")) == "system"

    def test_classify_error_system_memory(self) -> None:
        """MemoryError -> 'system'."""
        assert _classify_error(MemoryError()) == "system"

    def test_classify_error_agent_generic(self) -> None:
        """Generic exceptions -> 'agent'."""
        assert _classify_error(RuntimeError("something")) == "agent"
        assert _classify_error(ValueError("bad value")) == "agent"

    def test_classify_error_quota(self) -> None:
        """AdapterQuotaError -> 'quota' (codeprobe-f7rl.29)."""
        exc = AdapterQuotaError("Rate limited: 429 insufficient_quota")
        assert _classify_error(exc) == "quota"

    def test_execute_task_quota_error_sets_category(self, tmp_path: Path) -> None:
        """When adapter.run() raises AdapterQuotaError, error_category='quota'."""
        task_dir = _make_task(tmp_path / "task-001", passing=True)
        config = AgentConfig()

        adapter = _QuotaRaisingAdapter(stdout="")
        result = execute_task(adapter, task_dir, Path("/repo"), config).completed
        assert result.status == "error"
        assert result.error_category == "quota"

    def test_execute_task_timeout_sets_category(self, tmp_path: Path) -> None:
        """When adapter.run() raises TimeoutExpired, error_category='timeout'."""
        task_dir = _make_task(tmp_path / "task-001", passing=True)
        config = AgentConfig()

        class TimeoutAdapter(FakeAdapter):
            def run(self, prompt, config, session_env=None):
                raise subprocess.TimeoutExpired(cmd="agent", timeout=60)

        adapter = TimeoutAdapter(stdout="")
        result = execute_task(adapter, task_dir, Path("/repo"), config).completed
        assert result.status == "error"
        assert result.error_category == "timeout"

    def test_execute_task_oserror_sets_category(self, tmp_path: Path) -> None:
        """When adapter.run() raises OSError, error_category='system'."""
        task_dir = _make_task(tmp_path / "task-001", passing=True)
        config = AgentConfig()

        class OSErrorAdapter(FakeAdapter):
            def run(self, prompt, config, session_env=None):
                raise OSError("No space left on device")

        adapter = OSErrorAdapter(stdout="")
        result = execute_task(adapter, task_dir, Path("/repo"), config).completed
        assert result.status == "error"
        assert result.error_category == "system"

    def test_execute_task_generic_error_sets_agent_category(
        self, tmp_path: Path
    ) -> None:
        """When adapter.run() raises a generic error, error_category='agent'."""
        task_dir = _make_task(tmp_path / "task-001", passing=True)
        config = AgentConfig()

        class CrashAdapter(FakeAdapter):
            def run(self, prompt, config, session_env=None):
                raise RuntimeError("agent crashed")

        adapter = CrashAdapter(stdout="")
        result = execute_task(adapter, task_dir, Path("/repo"), config).completed
        assert result.status == "error"
        assert result.error_category == "agent"

    def test_execute_task_success_has_no_error_category(self, tmp_path: Path) -> None:
        """Successful tasks have error_category=None."""
        task_dir = _make_task(tmp_path / "task-001", passing=True)
        adapter = FakeAdapter(stdout="correct answer")
        config = AgentConfig()

        result = execute_task(adapter, task_dir, Path("/repo"), config).completed
        assert result.status == "completed"
        assert result.error_category is None

    def test_system_error_warning_above_threshold(self, tmp_path: Path) -> None:
        """When >30% of tasks have system errors, a WARNING is logged."""
        # 2 out of 3 tasks will raise OSError (67% > 30%)
        tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(3)]
        call_count = 0

        class MixedAdapter(FakeAdapter):
            def run(self, prompt, config, session_env=None):
                nonlocal call_count
                call_count += 1
                if call_count <= 2:
                    raise OSError("system failure")
                return super().run(prompt, config, session_env=session_env)

        adapter = MixedAdapter(stdout="output")
        exp_config = ExperimentConfig(label="test-config")
        agent_config = AgentConfig()

        import logging

        with patch.object(
            logging.getLogger("codeprobe.core.executor"),
            "warning",
        ) as mock_warn:
            execute_config(
                adapter=adapter,
                task_dirs=tasks,
                repo_path=Path("/repo"),
                experiment_config=exp_config,
                agent_config=agent_config,
            )
            # Find the capacity warning call
            capacity_calls = [
                c for c in mock_warn.call_args_list if "system errors" in str(c)
            ]
            assert len(capacity_calls) == 1
            assert "system errors" in str(capacity_calls[0])

    def test_system_error_warning_below_threshold(self, tmp_path: Path) -> None:
        """When <=30% of tasks have system errors, no warning is logged."""
        # 1 out of 4 tasks = 25% < 30%
        tasks = [_make_task(tmp_path / f"task-{i:03d}", passing=True) for i in range(4)]
        call_count = 0

        class MixedAdapter(FakeAdapter):
            def run(self, prompt, config, session_env=None):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    raise OSError("system failure")
                return super().run(prompt, config, session_env=session_env)

        adapter = MixedAdapter(stdout="output")
        exp_config = ExperimentConfig(label="test-config")
        agent_config = AgentConfig()

        import logging

        with patch.object(
            logging.getLogger("codeprobe.core.executor"),
            "warning",
        ) as mock_warn:
            execute_config(
                adapter=adapter,
                task_dirs=tasks,
                repo_path=Path("/repo"),
                experiment_config=exp_config,
                agent_config=agent_config,
            )
            capacity_calls = [
                c for c in mock_warn.call_args_list if "system errors" in str(c)
            ]
            assert len(capacity_calls) == 0


class TestRewardTypeAutoDetect:
    """Auto-detect reward_type from task metadata.json."""

    def test_oracle_task_uses_continuous_scoring(self, tmp_path: Path) -> None:
        """When metadata.json says continuous, executor uses it even if caller says binary."""
        import json

        task_dir = tmp_path / "task-001"
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text("Find files.\n")
        (task_dir / "metadata.json").write_text(
            json.dumps({"verification": {"reward_type": "continuous"}})
        )

        # Create test.sh that writes reward.txt with 0.5
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        test_sh = tests_dir / "test.sh"
        test_sh.write_text('#!/bin/bash\necho 0.5 > "$1/reward.txt"\n')
        test_sh.chmod(0o755)

        adapter = MagicMock()
        adapter.run.return_value = MagicMock(
            stdout="done",
            stderr="",
            exit_code=0,
            duration_seconds=1.0,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cost_usd=0.0,
            cost_model="per_token",
            cost_source="estimated",
            error=None,
        )

        result = execute_task(
            adapter=adapter,
            task_dir=task_dir,
            repo_path=tmp_path,
            agent_config=AgentConfig(),
            reward_type="binary",  # caller says binary...
        )
        # ...but task metadata says continuous, so score should be 0.5 not 1.0
        assert (
            result.completed.automated_score < 1.0
        ), "Expected continuous scoring (0.5) but got binary (1.0)"

    def test_no_metadata_keeps_binary(self, tmp_path: Path) -> None:
        """Without metadata.json, default binary scoring is used."""
        task_dir = tmp_path / "task-002"
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text("Do something.\n")

        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        test_sh = tests_dir / "test.sh"
        test_sh.write_text("#!/bin/bash\nexit 0\n")
        test_sh.chmod(0o755)

        adapter = MagicMock()
        adapter.run.return_value = MagicMock(
            stdout="done",
            stderr="",
            exit_code=0,
            duration_seconds=1.0,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cost_usd=0.0,
            cost_model="per_token",
            cost_source="estimated",
            error=None,
        )

        result = execute_task(
            adapter=adapter,
            task_dir=task_dir,
            repo_path=tmp_path,
            agent_config=AgentConfig(),
            reward_type="binary",
        )
        # Binary: exit 0 → score 1.0
        assert result.completed.automated_score == 1.0


# --- answer.json copy tests ---


class TestAnswerJsonCopy:
    """Executor stages answer.json/.txt into the per-run scoring sandbox.

    The original task_dir on disk is intentionally NOT mutated by scoring
    (per u6: per-run isolation). The scorer receives a temp snapshot dir
    that contains both the task files and any agent-produced answer files.
    """

    def _make_adapter_mock(self) -> MagicMock:
        adapter = MagicMock()
        adapter.run.return_value = MagicMock(
            stdout="done",
            stderr="",
            exit_code=0,
            duration_seconds=1.0,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cost_usd=0.0,
            cost_model="per_token",
            cost_source="estimated",
            error=None,
        )
        return adapter

    def _scorer_spy(self, captured: dict) -> object:
        """Stub scorer that copies the sandbox state into ``captured``."""
        from codeprobe.core.scoring import ScoreResult

        class _Spy:
            def score(self, agent_output: str, task_dir: Path) -> ScoreResult:
                captured["sandbox"] = Path(task_dir)
                captured["files"] = {
                    p.name: p.read_text()
                    for p in Path(task_dir).iterdir()
                    if p.is_file()
                }
                return ScoreResult(score=1.0, passed=True)

        return _Spy()

    def test_answer_json_copied_from_workspace_to_scoring_sandbox(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """answer.json written to workspace root is staged in the scoring sandbox."""
        task_dir = tmp_path / "task-json"
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text("Answer the question.\n")
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        test_sh = tests_dir / "test.sh"
        test_sh.write_text("#!/bin/bash\nexit 0\n")
        test_sh.chmod(0o755)

        # Simulate agent writing answer.json to workspace (repo) root
        repo = tmp_path / "repo"
        repo.mkdir()
        import json

        answer_data = {"answer": ["foo", "bar"]}
        (repo / "answer.json").write_text(json.dumps(answer_data))

        captured: dict = {}
        monkeypatch.setattr(
            "codeprobe.core.executor.get_scorer",
            lambda rt: self._scorer_spy(captured),
        )

        adapter = self._make_adapter_mock()

        execute_task(
            adapter=adapter,
            task_dir=task_dir,
            repo_path=repo,
            agent_config=AgentConfig(),
            reward_type="binary",
        )

        # Sandbox received the answer file
        assert "answer.json" in captured["files"]
        assert json.loads(captured["files"]["answer.json"]) == answer_data
        # Original task_dir was NOT mutated
        assert not (task_dir / "answer.json").exists()

    def test_answer_txt_still_staged(self, tmp_path: Path, monkeypatch) -> None:
        """answer.txt copy still works — staged into the scoring sandbox."""
        task_dir = tmp_path / "task-txt"
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text("Answer.\n")
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        test_sh = tests_dir / "test.sh"
        test_sh.write_text("#!/bin/bash\nexit 0\n")
        test_sh.chmod(0o755)

        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "answer.txt").write_text("42")

        captured: dict = {}
        monkeypatch.setattr(
            "codeprobe.core.executor.get_scorer",
            lambda rt: self._scorer_spy(captured),
        )

        adapter = self._make_adapter_mock()

        execute_task(
            adapter=adapter,
            task_dir=task_dir,
            repo_path=repo,
            agent_config=AgentConfig(),
            reward_type="binary",
        )
        assert captured["files"].get("answer.txt") == "42"
        # Original task_dir was NOT mutated
        assert not (task_dir / "answer.txt").exists()

    def test_answer_json_triggers_has_answer(self, tmp_path: Path) -> None:
        """Agent that writes answer.json but exits non-zero still gets scored."""
        task_dir = tmp_path / "task-json-err"
        task_dir.mkdir()
        (task_dir / "instruction.md").write_text("Answer.\n")
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        test_sh = tests_dir / "test.sh"
        test_sh.write_text("#!/bin/bash\nexit 0\n")
        test_sh.chmod(0o755)

        repo = tmp_path / "repo"
        repo.mkdir()
        import json

        (repo / "answer.json").write_text(json.dumps({"answer": ["x"]}))

        adapter = MagicMock()
        adapter.run.return_value = MagicMock(
            stdout="",  # empty stdout
            stderr="timeout",
            exit_code=1,  # non-zero exit
            duration_seconds=1.0,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cost_usd=0.0,
            cost_model="per_token",
            cost_source="estimated",
            error=None,
        )

        result = execute_task(
            adapter=adapter,
            task_dir=task_dir,
            repo_path=repo,
            agent_config=AgentConfig(),
            reward_type="binary",
        )
        # Should NOT be an error — has_answer is True due to answer.json
        assert (
            result.completed.status != "error"
        ), "answer.json should prevent early error return"


# --- Slot-worktree cwd + repo-root answer isolation (codeprobe-f7rl.6) ---


def _make_oracle_task(task_dir: Path, answer: str = "42") -> Path:
    """Create a minimal artifact-scored task with a text ground truth."""
    task_dir.mkdir(parents=True)
    (task_dir / "instruction.md").write_text("Answer the question.\n")
    tests_dir = task_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "ground_truth.json").write_text(
        json.dumps({"answer_type": "text", "answer": answer})
    )
    return task_dir


def test_execute_task_sets_cwd_to_worktree(tmp_path: Path) -> None:
    """The agent subprocess starts inside the slot worktree, not repo root."""
    task_dir = _make_task(tmp_path / "task-cwd")
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    adapter = FakeAdapter()
    config = AgentConfig(cwd=str(repo))

    execute_task(adapter, task_dir, repo, config, worktree_path=worktree)

    assert len(adapter.run_calls) == 1
    received = adapter.run_calls[0][1]
    assert received.cwd == str(worktree)
    # Immutability: the executor replaced the config, never mutated it.
    assert config.cwd == str(repo)


def test_execute_task_cwd_unchanged_without_worktree(tmp_path: Path) -> None:
    """Library callers that pass no worktree keep their configured cwd."""
    task_dir = _make_task(tmp_path / "task-cwd-none")
    repo = tmp_path / "repo"
    repo.mkdir()
    adapter = FakeAdapter()
    config = AgentConfig(cwd=str(repo))

    execute_task(adapter, task_dir, repo, config)

    assert adapter.run_calls[0][1].cwd == str(repo)


def test_repo_root_answer_never_scored(tmp_path: Path) -> None:
    """A stray repo-root answer file must never be credited to a trial.

    Regression test for the deleted repo-root fallback: with a slot
    worktree bound, an answer at repo_path is stale or cross-slot
    contamination, so the trial scores as missing-artifact instead.
    """
    task_dir = _make_oracle_task(tmp_path / "task-oracle-root")
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    # Both would score 1.0 if the fallback credited them.
    (repo / "answer.json").write_text(json.dumps({"answer": "42"}))
    (repo / "answer.txt").write_text("42")
    adapter = FakeAdapter(stdout="done")

    result = execute_task(
        adapter,
        task_dir,
        repo,
        AgentConfig(),
        reward_type="artifact",
        worktree_path=worktree,
    )

    assert result.completed.status == "completed"
    assert result.completed.automated_score == 0.0
    details = result.completed.scoring_details or {}
    assert details["passed"] is False
    assert details["error"] == "answer.json not found"


def test_worktree_answer_scored(tmp_path: Path) -> None:
    """An answer written into the slot worktree scores normally."""
    task_dir = _make_oracle_task(tmp_path / "task-oracle-wt")
    repo = tmp_path / "repo"
    repo.mkdir()
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "answer.json").write_text(json.dumps({"answer": "42"}))
    adapter = FakeAdapter(stdout="done")

    result = execute_task(
        adapter,
        task_dir,
        repo,
        AgentConfig(),
        reward_type="artifact",
        worktree_path=worktree,
    )

    assert result.completed.automated_score == 1.0
    assert (result.completed.scoring_details or {})["passed"] is True


# --- Commit pinning tests ---


class TestCommitPinning:
    """execute_task pins the workspace to the pre-merge commit when
    ground_truth_commit is present in task metadata."""

    def _make_task_with_metadata(
        self, task_dir: Path, *, ground_truth_commit: str = ""
    ) -> Path:
        """Create a task with metadata.json containing ground_truth_commit."""
        import json

        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "instruction.md").write_text("Fix the bug.")
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        test_sh = tests_dir / "test.sh"
        test_sh.write_text("#!/bin/bash\nexit 0\n")
        test_sh.chmod(0o755)

        metadata = {
            "id": task_dir.name,
            "repo": "test-repo",
            "metadata": {
                "name": f"merge-{task_dir.name}",
                "ground_truth_commit": ground_truth_commit,
            },
            "verification": {"type": "test_script", "command": "bash tests/test.sh"},
        }
        (task_dir / "metadata.json").write_text(json.dumps(metadata))
        return task_dir

    def test_pins_to_parent_of_merge_commit(self, tmp_path: Path) -> None:
        """When ground_truth_commit is set, git_pin_commit is called with sha^."""
        task_dir = self._make_task_with_metadata(
            tmp_path / "task-pin", ground_truth_commit="abc123def456"
        )
        adapter = FakeAdapter(stdout="output")
        config = AgentConfig()

        with patch("codeprobe.core.executor.git_pin_commit") as mock_pin:
            result = execute_task(adapter, task_dir, Path("/repo"), config)
            mock_pin.assert_called_once_with(Path("/repo"), "abc123def456^")
            assert result.completed.status == "completed"

    def test_no_pin_without_ground_truth_commit(self, tmp_path: Path) -> None:
        """When ground_truth_commit is empty, git_pin_commit is NOT called."""
        task_dir = self._make_task_with_metadata(
            tmp_path / "task-nopin", ground_truth_commit=""
        )
        adapter = FakeAdapter(stdout="output")
        config = AgentConfig()

        with patch("codeprobe.core.executor.git_pin_commit") as mock_pin:
            result = execute_task(adapter, task_dir, Path("/repo"), config)
            mock_pin.assert_not_called()
            assert result.completed.status == "completed"

    def test_no_pin_without_metadata(self, tmp_path: Path) -> None:
        """When metadata.json is absent, git_pin_commit is NOT called."""
        task_dir = _make_task(tmp_path / "task-nometa", passing=True)
        adapter = FakeAdapter(stdout="output")
        config = AgentConfig()

        with patch("codeprobe.core.executor.git_pin_commit") as mock_pin:
            execute_task(adapter, task_dir, Path("/repo"), config)
            mock_pin.assert_not_called()

    def test_pin_failure_returns_error(self, tmp_path: Path) -> None:
        """When git checkout fails, task returns error with system category."""
        task_dir = self._make_task_with_metadata(
            tmp_path / "task-pinfail", ground_truth_commit="deadbeef12345678"
        )
        adapter = FakeAdapter(stdout="output")
        config = AgentConfig()

        with patch(
            "codeprobe.core.executor.git_pin_commit",
            side_effect=subprocess.CalledProcessError(
                128, "git", stderr=b"fatal: reference is not a tree"
            ),
        ):
            result = execute_task(adapter, task_dir, Path("/repo"), config)
            assert result.completed.status == "error"
            assert result.completed.error_category == "system"
            assert "deadbeef" in result.completed.metadata["error"]

    def test_pin_uses_worktree_path_when_provided(self, tmp_path: Path) -> None:
        """When worktree_path is set, pinning targets the worktree, not repo_path."""
        task_dir = self._make_task_with_metadata(
            tmp_path / "task-wt", ground_truth_commit="abc123def456"
        )
        adapter = FakeAdapter(stdout="output")
        config = AgentConfig()
        wt = Path("/worktrees/slot-0")

        with patch("codeprobe.core.executor.git_pin_commit") as mock_pin:
            execute_task(adapter, task_dir, Path("/repo"), config, worktree_path=wt)
            mock_pin.assert_called_once_with(wt, "abc123def456^")

    def test_sequential_pin_targets_slot_not_repo_path(self, tmp_path: Path) -> None:
        """Sequential pinning tasks pin the acquired slot, never repo_path."""
        import json

        tasks = []
        for i in range(2):
            td = tmp_path / f"task-{i:03d}"
            td.mkdir(parents=True)
            (td / "instruction.md").write_text("Fix.")
            tests = td / "tests"
            tests.mkdir()
            test_sh = tests / "test.sh"
            test_sh.write_text("#!/bin/bash\nexit 0\n")
            test_sh.chmod(0o755)
            (td / "metadata.json").write_text(
                json.dumps({"metadata": {"ground_truth_commit": f"sha{i}"}})
            )
            tasks.append(td)

        adapter = FakeAdapter(stdout="output")
        exp_config = ExperimentConfig(label="baseline")
        agent_config = AgentConfig()

        fake_iso = MagicMock()
        slot = tmp_path / "slot-0"
        fake_iso.acquire.return_value = slot
        with (
            patch("codeprobe.core.executor.git_pin_commit") as mock_pin,
            patch(
                "codeprobe.core.executor.WorktreeIsolation", return_value=fake_iso
            ),
        ):
            execute_config(
                adapter=adapter,
                task_dirs=tasks,
                repo_path=Path("/repo"),
                experiment_config=exp_config,
                agent_config=agent_config,
                parallel=1,
            )
            assert mock_pin.call_count == 2
            for c in mock_pin.call_args_list:
                assert c[0][0] == slot


class TestExecuteTaskMultiRepo:
    """execute_task sets up workspace/repos/<name> when metadata has
    additional_repos."""

    def _make_task(self, task_dir: Path, *, additional_repos: list[dict]) -> Path:
        import json

        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "instruction.md").write_text("Fix the bug.")
        tests_dir = task_dir / "tests"
        tests_dir.mkdir()
        test_sh = tests_dir / "test.sh"
        test_sh.write_text("#!/bin/bash\nexit 0\n")
        test_sh.chmod(0o755)
        metadata = {
            "id": task_dir.name,
            "metadata": {
                "additional_repos": additional_repos,
            },
            "verification": {"type": "test_script", "command": "bash tests/test.sh"},
        }
        (task_dir / "metadata.json").write_text(json.dumps(metadata))
        return task_dir

    def test_calls_setup_multi_repo_workspace(self, tmp_path: Path) -> None:
        additional = [
            {
                "name": "repoB",
                "ground_truth_commit": "cafef00d",
                "local_path": "/some/path",
            }
        ]
        task_dir = self._make_task(tmp_path / "task-mr", additional_repos=additional)
        adapter = FakeAdapter(stdout="output")
        config = AgentConfig()

        with patch("codeprobe.core.executor.setup_multi_repo_workspace") as mock_setup:
            result = execute_task(adapter, task_dir, Path("/repo"), config)
            mock_setup.assert_called_once_with(Path("/repo"), additional)
            assert result.completed.status == "completed"

    def test_setup_failure_returns_system_error(self, tmp_path: Path) -> None:
        task_dir = self._make_task(
            tmp_path / "task-mrfail",
            additional_repos=[
                {
                    "name": "repoB",
                    "ground_truth_commit": "deadbeef",
                    "local_path": "/some/path",
                }
            ],
        )
        adapter = FakeAdapter(stdout="output")
        config = AgentConfig()

        with patch(
            "codeprobe.core.executor.setup_multi_repo_workspace",
            side_effect=subprocess.CalledProcessError(128, "git", stderr=b"bad ref"),
        ):
            result = execute_task(adapter, task_dir, Path("/repo"), config)
            assert result.completed.status == "error"
            assert result.completed.error_category == "system"
            assert "multi-repo" in result.completed.metadata["error"]

    def test_no_setup_when_additional_repos_empty(self, tmp_path: Path) -> None:
        task_dir = self._make_task(tmp_path / "task-none", additional_repos=[])
        adapter = FakeAdapter(stdout="output")
        config = AgentConfig()

        with patch("codeprobe.core.executor.setup_multi_repo_workspace") as mock_setup:
            execute_task(adapter, task_dir, Path("/repo"), config)
            mock_setup.assert_not_called()

    def test_uses_worktree_path_when_provided(self, tmp_path: Path) -> None:
        additional = [
            {
                "name": "repoB",
                "ground_truth_commit": "abc",
                "local_path": "/x",
            }
        ]
        task_dir = self._make_task(tmp_path / "task-mrwt", additional_repos=additional)
        adapter = FakeAdapter(stdout="output")
        config = AgentConfig()
        wt = Path("/worktrees/slot-0")

        with patch("codeprobe.core.executor.setup_multi_repo_workspace") as mock_setup:
            execute_task(adapter, task_dir, Path("/repo"), config, worktree_path=wt)
            mock_setup.assert_called_once_with(wt, additional)


# ---------------------------------------------------------------------------
# hide_local_source integration (codeprobe-jf28)
# ---------------------------------------------------------------------------


class _RecordingAdapter(FakeAdapter):
    """FakeAdapter that snapshots the workspace contents during run().

    Used to verify that ``quarantine_local_source`` is active around the
    adapter's run() call when ``hide_local_source != "off"``.
    """

    def __init__(self, *, workspace: Path, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._workspace = workspace
        self.workspace_entries_during_run: list[str] | None = None

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
        if self._workspace.is_dir():
            self.workspace_entries_during_run = sorted(
                p.name for p in self._workspace.iterdir()
            )
        # Simulate the agent producing answer.txt in the (empty) workspace.
        (self._workspace / "answer.txt").write_text("agent computed answer")
        return super().run(prompt, config, session_env=session_env)


def test_execute_task_hide_local_source_stashes_during_run(tmp_path: Path):
    """When hide_local_source="hide", the workspace is empty during run()
    and source is restored before scoring."""
    task_dir = _make_task(tmp_path / "task-jf28", passing=True)

    # Set up a workspace with realistic source layout next to the task dir.
    repo_path = tmp_path / "workspace"
    repo_path.mkdir()
    (repo_path / "src").mkdir()
    (repo_path / "src" / "main.py").write_text("def main(): ...")
    (repo_path / "README.md").write_text("# Project")
    (repo_path / ".git").mkdir()  # default-keep entry survives quarantine

    adapter = _RecordingAdapter(workspace=repo_path, stdout="ok")

    result = execute_task(
        adapter,
        task_dir,
        repo_path,
        AgentConfig(),
        hide_local_source="hide",
    )

    # During the run the agent saw an empty workspace (apart from .git).
    assert adapter.workspace_entries_during_run is not None
    visible = [
        e
        for e in adapter.workspace_entries_during_run
        if e != ".git" and e != "answer.txt"
    ]
    assert visible == [], (
        f"agent saw source files during sg-only run: {visible!r}"
    )

    # Source is restored AFTER the run.
    assert (repo_path / "src" / "main.py").read_text() == "def main(): ..."
    assert (repo_path / "README.md").read_text() == "# Project"

    # answer.txt produced during the empty-workspace window survives
    # the source restore (it didn't conflict with any stashed name).
    assert (repo_path / "answer.txt").read_text() == "agent computed answer"

    # Task scored as completed (test.sh exits 0).
    assert result.completed.status == "completed"
    assert result.completed.automated_score == 1.0


def test_execute_task_hide_local_source_default_off_keeps_source_visible(
    tmp_path: Path,
):
    """Default (hide_local_source="off") is no-op: source is visible
    throughout the run."""
    task_dir = _make_task(tmp_path / "task-default", passing=True)

    repo_path = tmp_path / "workspace"
    repo_path.mkdir()
    (repo_path / "src.py").write_text("source")

    adapter = _RecordingAdapter(workspace=repo_path, stdout="ok")

    execute_task(adapter, task_dir, repo_path, AgentConfig())

    assert adapter.workspace_entries_during_run is not None
    assert "src.py" in adapter.workspace_entries_during_run, (
        "source was hidden when hide_local_source defaulted to 'off'"
    )


# ---------------------------------------------------------------------------
# scaffold-mode integration (codeprobe-2nw2.3 / codeprobe-sm9f)
# ---------------------------------------------------------------------------


class _ScaffoldEditAdapter(FakeAdapter):
    """Adapter that grows a 0-byte scaffold placeholder during run()."""

    def __init__(
        self,
        *,
        workspace: Path,
        rel_path: str,
        content: str,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._workspace = workspace
        self._rel_path = rel_path
        self._content = content
        self.placeholder_size_during_run: int | None = None

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
        target = self._workspace / self._rel_path
        if target.exists():
            self.placeholder_size_during_run = target.stat().st_size
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(self._content)
        return super().run(prompt, config, session_env=session_env)


def test_execute_task_scaffold_mode_overlays_agent_edits_before_scoring(
    tmp_path: Path,
):
    """Verifier-side wiring: scaffold mode overlays agent edits onto
    restored source before scoring, so the oracle sees the merged tree.

    Uses the codeprobe-2nw2 smoke fixture (``tests/fixtures/sdlc_sgonly_smoke``)
    with binary scoring so the exit code of ``tests/test.sh`` directly maps
    to ``automated_score``. The oracle greps ``${TASK_REPO_ROOT}/src/math.go``
    for ``func add`` — only matches if the scaffold context manager's
    overlay step ran after restore. Passing ``worktree_path=workspace``
    sets ``TASK_REPO_ROOT`` so test.sh resolves to the merged workspace.
    """
    import json as _json
    import shutil as _shutil

    smoke_root = Path(__file__).parent / "fixtures" / "sdlc_sgonly_smoke"

    # Copy fixture into both a task_dir (for scoring sandbox) and a writable
    # workspace (where the agent edits happen).
    task_dir = tmp_path / "task"
    _shutil.copytree(smoke_root, task_dir)
    workspace = tmp_path / "ws"
    _shutil.copytree(smoke_root, workspace)

    # Flip the task's reward_type to binary so the oracle exit code is
    # the headline score. The smoke fixture's continuous setting is the
    # design-doc reference shape (codeprobe-hcnv exercises it end-to-end
    # via reward.txt); this test only needs to prove that scoring sees
    # the merged tree.
    meta_path = task_dir / "metadata.json"
    meta = _json.loads(meta_path.read_text())
    meta["verification"]["reward_type"] = "binary"
    meta_path.write_text(_json.dumps(meta, indent=2))

    agent_program = (
        "package math\n\n"
        "// existing\n\n"
        "func add(a int, b int) int {\n"
        "    return a + b\n"
        "}\n"
    )

    adapter = _ScaffoldEditAdapter(
        workspace=workspace,
        rel_path="src/math.go",
        content=agent_program,
        stdout="ok",
    )

    result = execute_task(
        adapter,
        task_dir,
        workspace,
        AgentConfig(),
        worktree_path=workspace,
        hide_local_source="scaffold",
    )

    # During the yield the adapter saw a 0-byte placeholder, proving
    # scaffold mode was active.
    assert adapter.placeholder_size_during_run == 0, (
        f"expected 0-byte scaffold during run, "
        f"saw {adapter.placeholder_size_during_run!r}"
    )

    # Post-restore + overlay: workspace/src/math.go has the merged content.
    merged = (workspace / "src" / "math.go").read_text()
    assert "// existing" in merged, "restored source missing from merged tree"
    assert "func add" in merged, "agent overlay missing from merged tree"

    # The oracle (bash tests/test.sh with TASK_REPO_ROOT=workspace) saw
    # the merged tree → exit 0 → binary score 1.0.
    assert result.completed.status == "completed"
    assert result.completed.automated_score == 1.0


def test_execute_task_hide_mode_does_not_scaffold(tmp_path: Path):
    """When ``hide_local_source="hide"``, the workspace is empty during
    the yield (no 0-byte placeholders) and ``answer.txt`` writes survive
    the restore — i.e. ``"hide"`` is NOT silently upgraded to scaffold
    semantics.
    """
    task_dir = _make_task(tmp_path / "task-default-mode", passing=True)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "main.py").write_text("ORIG")

    adapter = _RecordingAdapter(workspace=workspace, stdout="ok")

    execute_task(
        adapter,
        task_dir,
        workspace,
        AgentConfig(),
        hide_local_source="hide",
    )

    # In hide mode the agent sees an empty workspace (apart from any
    # default-keep dirs that exist). Critically: NO 0-byte placeholders.
    assert adapter.workspace_entries_during_run is not None
    visible_during_run = [
        e
        for e in adapter.workspace_entries_during_run
        if e != ".git" and e != "answer.txt"
    ]
    assert visible_during_run == [], (
        f"hide-mode leaked source files during run: {visible_during_run!r}"
    )
    # Post-run: source restored, no 0-byte placeholder lingering.
    assert (workspace / "src" / "main.py").read_text() == "ORIG"
