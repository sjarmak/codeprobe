"""Shared base for agent adapters — eliminates duplicated run/preflight logic."""

from __future__ import annotations

import shutil
import subprocess
import time
from abc import abstractmethod

from codeprobe.adapters.protocol import (
    AdapterSetupError,
    AgentConfig,
    AgentOutput,
)


class BaseAdapter:
    """Base class for CLI-based agent adapters.

    Subclasses set ``_binary_name`` and ``_install_hint``, then implement
    ``build_command``.  The Protocol requires ``name``, ``preflight``, and
    ``run``; ``find_binary`` and ``build_command`` are BaseAdapter helpers.
    """

    _binary_name: str
    _install_hint: str

    @property
    def name(self) -> str:
        return self._binary_name

    def find_binary(self) -> str | None:
        return shutil.which(self._binary_name)

    def _require_binary(self) -> str:
        """Return binary path or raise AdapterSetupError."""
        binary = self.find_binary()
        if binary is None:
            raise AdapterSetupError(f"{self._binary_name} CLI not found")
        return binary

    def preflight(self, config: AgentConfig) -> list[str]:
        issues: list[str] = []
        if self.find_binary() is None:
            issues.append(self._install_hint)
        return issues

    @abstractmethod
    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        ...

    def parse_output(
        self, result: subprocess.CompletedProcess[str], duration: float
    ) -> AgentOutput:
        """Convert subprocess result to AgentOutput.

        Subclasses override to extract tokens, cost, etc. from agent output.
        """
        return AgentOutput(
            stdout=result.stdout,
            stderr=result.stderr or None,
            exit_code=result.returncode,
            duration_seconds=duration,
        )

    def run(self, prompt: str, config: AgentConfig) -> AgentOutput:
        cmd = self.build_command(prompt, config)
        start = time.monotonic()

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            return AgentOutput(
                stdout=exc.stdout if isinstance(exc.stdout, str) else "",
                stderr=exc.stderr if isinstance(exc.stderr, str) else None,
                exit_code=-1,
                duration_seconds=duration,
                error=f"Agent timed out after {config.timeout_seconds}s",
            )
        except FileNotFoundError as exc:
            raise AdapterSetupError(
                f"Binary not found at runtime: {exc}"
            ) from exc

        duration = time.monotonic() - start

        try:
            return self.parse_output(result, duration)
        except Exception as exc:
            return AgentOutput(
                stdout=result.stdout,
                stderr=result.stderr or None,
                exit_code=result.returncode,
                duration_seconds=duration,
                error=f"Output parse failed: {exc}",
            )
