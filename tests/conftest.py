"""Shared test fixtures for codeprobe tests."""

from __future__ import annotations

from codeprobe.adapters.protocol import AgentConfig, AgentOutput


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
        binary: str | None = "/usr/bin/fake-agent",
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._exit_code = exit_code
        self._duration = duration
        self._cost_usd = cost_usd
        self._cost_model = cost_model
        self._binary = binary
        self.run_calls: list[tuple[str, AgentConfig]] = []

    @property
    def name(self) -> str:
        return "fake"

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
