"""Shared base for agent adapters — eliminates duplicated run/preflight logic."""

from __future__ import annotations

import shutil
import subprocess
import time
from abc import abstractmethod

from codeprobe.adapters.protocol import AgentConfig, AgentOutput


class BaseAdapter:
    """Base class for CLI-based agent adapters.

    Subclasses set ``_binary_name`` and ``_install_hint``, then implement
    ``build_command``.  The ``run``, ``preflight``, and ``find_binary``
    methods are shared.
    """

    _binary_name: str
    _install_hint: str

    @property
    def name(self) -> str:
        return self._binary_name

    def find_binary(self) -> str | None:
        return shutil.which(self._binary_name)

    def _require_binary(self) -> str:
        """Return binary path or raise RuntimeError."""
        binary = self.find_binary()
        if binary is None:
            raise RuntimeError(f"{self._binary_name} CLI not found")
        return binary

    def preflight(self, config: AgentConfig) -> list[str]:
        issues: list[str] = []
        if self.find_binary() is None:
            issues.append(self._install_hint)
        return issues

    @abstractmethod
    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        ...

    def run(self, prompt: str, config: AgentConfig) -> AgentOutput:
        cmd = self.build_command(prompt, config)
        start = time.monotonic()

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
        )

        duration = time.monotonic() - start

        return AgentOutput(
            stdout=result.stdout,
            stderr=result.stderr or None,
            exit_code=result.returncode,
            duration_seconds=duration,
        )
