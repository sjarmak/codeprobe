"""Shared base for agent adapters — eliminates duplicated run/preflight logic."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from abc import abstractmethod

from codeprobe.adapters.protocol import (
    AdapterSetupError,
    AgentConfig,
    AgentOutput,
)

# Only these env vars are forwarded to agent subprocesses.
# Keeps secrets (OPENAI_API_KEY, AWS_SECRET_*, etc.) out of the child
# unless explicitly listed here.
_ADAPTER_ENV_WHITELIST: frozenset[str] = frozenset(
    {
        # System essentials
        "PATH",
        "HOME",
        "LANG",
        "TERM",
        "TMPDIR",
        "LC_ALL",
        # Agent-specific API keys (required by the adapters)
        "ANTHROPIC_API_KEY",
        "CLAUDE_CONFIG_DIR",
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "COPILOT_API_KEY",
        # Python toolchain
        "VIRTUAL_ENV",
        "PYTHONPATH",
        # Node/npm (for copilot CLI)
        "NODE_PATH",
        "NPM_CONFIG_PREFIX",
        # Go toolchain
        "GOPATH",
        "GOROOT",
        # Rust toolchain
        "CARGO_HOME",
        "RUSTUP_HOME",
    }
)


def _adapter_safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a filtered environment for agent subprocesses.

    Only passes whitelisted vars — prevents leaking secrets from parent env.
    """
    env = {k: v for k, v in os.environ.items() if k in _ADAPTER_ENV_WHITELIST}
    if extra:
        env.update(extra)
    return env


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

    def isolate_session(self, slot_id: int) -> dict[str, str]:
        """Default: no session isolation env overrides."""
        return {}

    @abstractmethod
    def build_command(self, prompt: str, config: AgentConfig) -> list[str]: ...

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

    def _write_mcp_config(self, config: AgentConfig) -> str | None:
        """Write MCP config to a temp file if present. Returns path or None.

        Expands ``${VAR}`` references in string values from the environment
        so experiment.json can reference secrets without hardcoding them.
        """
        if not config.mcp_config:
            return None
        expanded = json.loads(os.path.expandvars(json.dumps(config.mcp_config)))
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", prefix="codeprobe-mcp-", delete=False
        )
        json.dump(expanded, tmp)
        tmp.close()
        return tmp.name

    def run(self, prompt: str, config: AgentConfig) -> AgentOutput:
        cmd = self.build_command(prompt, config)
        mcp_tmpfile: str | None = None

        # Find and track MCP temp file for cleanup
        for flag in ("--mcp-config", "--additional-mcp-config"):
            if flag in cmd:
                idx = cmd.index(flag)
                if idx + 1 < len(cmd):
                    path = cmd[idx + 1]
                    if path.startswith(tempfile.gettempdir()):
                        mcp_tmpfile = path

        start = time.monotonic()

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
                cwd=config.cwd,
                env=_adapter_safe_env(),
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
            raise AdapterSetupError(f"Binary not found at runtime: {exc}") from exc
        finally:
            if mcp_tmpfile:
                try:
                    os.unlink(mcp_tmpfile)
                except OSError:
                    pass

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
