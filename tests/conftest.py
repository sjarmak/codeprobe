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
        binary: str | None = "/usr/bin/fake-agent",
    ) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self._exit_code = exit_code
        self._duration = duration
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

    def run(self, prompt: str, config: AgentConfig) -> AgentOutput:
        self.run_calls.append((prompt, config))
        return AgentOutput(
            stdout=self._stdout,
            stderr=self._stderr,
            exit_code=self._exit_code,
            duration_seconds=self._duration,
        )
