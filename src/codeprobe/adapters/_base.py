"""Shared base for agent adapters — eliminates duplicated run/preflight logic."""

from __future__ import annotations

import dataclasses
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
        "USER",
        "LOGNAME",
        "LANG",
        "TERM",
        "TMPDIR",
        "LC_ALL",
        # XDG / desktop-session env — required for Linux keyring (libsecret)
        # lookups so OAuth/keychain-auth agents can reach the session bus
        # when CLAUDE_CONFIG_DIR is overridden for isolation.
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        # Codeprobe sandbox signal (eval harness sets this)
        "CODEPROBE_SANDBOX",
        # Agent-specific API keys (required by the adapters)
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
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


def _decode_timeout_output(raw: str | bytes | None) -> str:
    """Decode stdout/stderr from a TimeoutExpired exception.

    The exception may carry ``str``, ``bytes``, or ``None`` depending on
    how ``subprocess.run`` was called and how the process was killed.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


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

    def parse_output(self, result: subprocess.CompletedProcess[str], duration: float) -> AgentOutput:
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
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", prefix="codeprobe-mcp-", delete=False)
        json.dump(expanded, tmp)
        tmp.close()
        return tmp.name

    def run(
        self,
        prompt: str,
        config: AgentConfig,
        session_env: dict[str, str] | None = None,
    ) -> AgentOutput:
        cmd = self.build_command(prompt, config)
        mcp_tmpfile: str | None = None

        # Find and track MCP temp file for cleanup
        for flag in ("--mcp-config", "--additional-mcp-config"):
            if flag in cmd:
                idx = cmd.index(flag)
                if idx + 1 < len(cmd):
                    path = cmd[idx + 1]
                    # Copilot's --additional-mcp-config takes an @-prefixed
                    # file reference; strip it so cleanup still tracks the file.
                    candidate = path[1:] if path.startswith("@") else path
                    if candidate.startswith(tempfile.gettempdir()):
                        mcp_tmpfile = candidate

        start = time.monotonic()

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.timeout_seconds,
                cwd=config.cwd,
                env=_adapter_safe_env(session_env) if session_env else None,
            )
        except subprocess.TimeoutExpired as exc:
            duration = time.monotonic() - start
            timeout_error = f"Agent timed out after {config.timeout_seconds}s"

            raw_stdout = _decode_timeout_output(exc.stdout)
            raw_stderr = _decode_timeout_output(exc.stderr) or None

            if raw_stdout:
                try:
                    partial_result = subprocess.CompletedProcess(
                        args=cmd,
                        returncode=-1,
                        stdout=raw_stdout,
                        stderr=raw_stderr or "",
                    )
                    parsed = self.parse_output(partial_result, duration)
                    merged_error = timeout_error
                    if parsed.error:
                        merged_error = f"{timeout_error}; {parsed.error}"
                    # Rebuild via replace() so every telemetry field the
                    # partial parse recovered survives — dropping fields
                    # here previously lost e.g. a quota stub's category
                    # inside a timed-out trial (codeprobe-f7rl.29). An
                    # adapter-declared category (quota) outranks the
                    # generic timeout classification.
                    return dataclasses.replace(
                        parsed,
                        exit_code=-1,
                        duration_seconds=duration,
                        error=merged_error,
                        error_category=parsed.error_category or "timeout",
                    )
                except Exception as parse_exc:
                    timeout_error = f"{timeout_error}; parse_output failed: {parse_exc}"

            return AgentOutput(
                stdout=raw_stdout,
                stderr=raw_stderr,
                exit_code=-1,
                duration_seconds=duration,
                error=timeout_error,
                error_category="timeout",
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
