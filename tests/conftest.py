"""Shared test fixtures for codeprobe tests."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from codeprobe.adapters.protocol import (
    AdapterCapabilities,
    AgentConfig,
    AgentOutput,
)


class PassthroughIsolation:
    """WorktreeIsolation stand-in whose slot IS the repo path.

    execute_config/execute_task route every run through a worktree slot
    (codeprobe-f7rl.2), which requires a real git checkout at repo_path.
    Legacy tests built around a nonexistent repo path (``Path("/repo")``)
    opt into this fake via the ``fake_worktree_isolation`` fixture: acquire()
    hands back repo_path itself, reproducing the exact pre-worktree
    semantics those tests were written against. The never-mutate-the-primary-
    checkout property is proven separately with real repos in
    tests/test_executor_worktree_safety.py.
    """

    def __init__(self, repo_path: Path, pool_size: int, namespace: str = "") -> None:
        self._repo_path = repo_path

    def acquire(self) -> Path:
        return self._repo_path

    def reset(self, workspace: Path) -> None:
        pass

    def release(self, workspace: Path) -> None:
        pass

    def cleanup(self) -> None:
        pass


@pytest.fixture
def fake_worktree_isolation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the executor's WorktreeIsolation with PassthroughIsolation."""
    monkeypatch.setattr(
        "codeprobe.core.executor.WorktreeIsolation", PassthroughIsolation
    )


@pytest.fixture(autouse=True)
def _containment_consent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the containment gate deterministic across test hosts.

    ``codeprobe run`` hard-refuses outside a container unless the user
    consents (codeprobe-f7rl.3, ``codeprobe.core.containment``). Without
    this fixture, run-path tests would pass inside Docker CI and fail on a
    bare developer host. Setting the documented USER-set consent signal
    simulates a contained environment suite-wide; the containment gate
    tests explicitly delete it and monkeypatch
    ``codeprobe.core.sandbox.is_sandboxed`` to exercise both refusal and
    consent branches.
    """
    monkeypatch.setenv("CODEPROBE_SANDBOX", "1")


@pytest.fixture(autouse=True)
def _reset_codeprobe_logger():
    """Ensure the codeprobe logger is clean before and after every test.

    _configure_logging() sets propagate=False and attaches a StreamHandler.
    Without cleanup, caplog-based assertions in later tests silently fail
    because records never propagate to pytest's capture handler.
    """
    yield
    logger = logging.getLogger("codeprobe")
    for h in list(logger.handlers):
        logger.removeHandler(h)
    logger.propagate = True
    logger.setLevel(logging.WARNING)


class FakeAdapter:
    """A minimal AgentAdapter for testing — configurable responses."""

    def __init__(
        self,
        *,
        stdout: str = "fake output",
        stderr: str | None = None,
        exit_code: int = 0,
        duration: float = 1.0,
        cost_usd: float | None = None,
        cost_model: str = "unknown",
        error: str | None = None,
        error_category: str | None = None,
        error_terminal: bool = False,
        num_turns: int | None = None,
        result_subtype: str | None = None,
        duration_api_ms: int | None = None,
        binary: str | None = "/usr/bin/fake-agent",
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._exit_code = exit_code
        self._duration = duration
        self._cost_usd = cost_usd
        self._cost_model = cost_model
        self._error = error
        self._error_category = error_category
        self._error_terminal = error_terminal
        self._num_turns = num_turns
        self._result_subtype = result_subtype
        self._duration_api_ms = duration_api_ms
        self._binary = binary
        self.run_calls: list[tuple[str, AgentConfig]] = []

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self) -> AdapterCapabilities:
        # The fake stands in for a fully capable agent so run-path tests
        # exercising mcp_config / tool policy / max_turns aren't refused
        # by the fail-closed capability preflight (codeprobe-f7rl.26).
        return AdapterCapabilities(
            mcp_config=True,
            allowed_tools=True,
            disallowed_tools=True,
            max_turns=True,
            permission_mode=True,
            workspace_cwd=True,
            timeout=True,
        )

    def find_binary(self) -> str | None:
        return self._binary

    def preflight(self, config: AgentConfig) -> list[str]:
        if self._binary is None:
            return ["Fake agent binary not found"]
        return []

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        return ["fake-agent", "-p", prompt]

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
        self.run_calls.append((prompt, config))
        return AgentOutput(
            stdout=self._stdout,
            stderr=self._stderr,
            exit_code=self._exit_code,
            duration_seconds=self._duration,
            cost_usd=self._cost_usd,
            cost_model=self._cost_model,
            error=self._error,
            error_category=self._error_category,
            error_terminal=self._error_terminal,
            num_turns=self._num_turns,
            result_subtype=self._result_subtype,
            duration_api_ms=self._duration_api_ms,
        )

    def isolate_session(self, slot_id: int) -> dict[str, str]:
        return {}


class SequentialCostAdapter(FakeAdapter):
    """FakeAdapter that returns different costs for each run call."""

    def __init__(self, costs: list[tuple[float | None, str]], **kwargs) -> None:
        super().__init__(**kwargs)
        self._costs = costs
        self._call_index = 0

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
        if self._call_index >= len(self._costs):
            raise AssertionError(
                f"SequentialCostAdapter: run() called {self._call_index + 1} times "
                f"but only {len(self._costs)} costs were provided"
            )
        cost_usd, cost_model = self._costs[self._call_index]
        self._call_index += 1
        self._cost_usd = cost_usd
        self._cost_model = cost_model
        return super().run(prompt, config, session_env=session_env)
