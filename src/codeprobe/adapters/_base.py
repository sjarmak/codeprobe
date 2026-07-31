"""Shared base for agent adapters — eliminates duplicated run/preflight logic."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from abc import abstractmethod
from pathlib import Path

from codeprobe.adapters.protocol import (
    AdapterSetupError,
    AgentConfig,
    AgentOutput,
)
from codeprobe.config.mcp_runtime import resolve_mcp_runtime_config
from codeprobe.core import containment
from codeprobe.sandbox.agent_container import containerize_argv
from codeprobe.sandbox.runner import agent_image_reference

logger = logging.getLogger(__name__)

# Only these env vars are forwarded to agent subprocesses, on EVERY
# dispatch path (serial and parallel alike) — keeps secrets
# (AWS_SECRET_ACCESS_KEY, unlisted API keys, etc.) out of the child and
# guarantees serial and parallel arms see the same environment.
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
        # User-set containment consent signal (codeprobe never sets this)
        "CODEPROBE_SANDBOX",
        # Agent-specific API keys (required by the adapters)
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "GITHUB_TOKEN",
        "OPENAI_API_KEY",
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "COPILOT_OFFLINE",
        "COPILOT_PROVIDER_BASE_URL",
        "COPILOT_PROVIDER_API_KEY",
        "COPILOT_PROVIDER_TYPE",
        "COPILOT_MODEL",
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
        # Corporate proxy / TLS trust — enterprise networks front all
        # egress with a proxy and a private CA; without these the agent
        # works serially (full-env inherit was the old behavior) but
        # breaks under isolation. NODE_OPTIONS is deliberately excluded
        # (arbitrary code-injection surface).
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        # Anthropic gateway routing — enterprises fronting the Anthropic
        # API (LLM gateways) set these; same works-serial/breaks-parallel
        # failure class as the proxy vars.
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_AUTH_TOKEN",
    }
)


# Whitelist keys NOT forwarded into the agent container via ``-e KEY``
# (codeprobe-f7rl.5). Only the slot worktree and the session config dir are
# identity-mounted, so host-path-shaped values (toolchain roots, XDG/DBus
# session paths, TMPDIR) would dangle inside the container, and forwarding
# the host PATH/HOME would shadow the image's own toolchain.
_CONTAINER_ENV_EXCLUDED: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "TMPDIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "XDG_RUNTIME_DIR",
        "XDG_DATA_HOME",
        "XDG_CONFIG_HOME",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "NODE_PATH",
        "NPM_CONFIG_PREFIX",
        "GOPATH",
        "GOROOT",
        "CARGO_HOME",
        "RUSTUP_HOME",
        # CA-trust vars hold host filesystem paths that are not mounted
        # into the container; a dangling SSL_CERT_FILE hard-fails TLS in
        # most stacks. Proxy URL vars stay in the passthrough.
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
    }
)

# Env keys the agent container receives (``-e KEY`` passthrough): the
# adapter whitelist minus the host-path-shaped exclusions above. Credentials
# (ANTHROPIC_API_KEY, CLAUDE_CODE_OAUTH_TOKEN, ...) and locale/identity keys
# survive; the value is copied by the engine from its client environment so
# secrets never appear in the argv.
_CONTAINER_ENV_KEYS: frozenset[str] = _ADAPTER_ENV_WHITELIST - _CONTAINER_ENV_EXCLUDED


_PRIVATE_CA_FILE_ENV_KEYS: tuple[str, ...] = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)
_PRIVATE_CA_DIR_ENV_KEYS: tuple[str, ...] = ("SSL_CERT_DIR",)
_CONTAINER_CA_ROOT = Path("/etc/codeprobe/ca")
_AGENT_TERMINATE_GRACE_SECONDS = 2.0
_AGENT_KILL_GRACE_SECONDS = 2.0


def _agent_process_groups_supported() -> bool:
    return os.name == "posix"


def _adapter_safe_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a filtered environment for agent subprocesses.

    Only passes whitelisted vars — prevents leaking secrets from parent env.
    """
    env = {k: v for k, v in os.environ.items() if k in _ADAPTER_ENV_WHITELIST}
    if extra:
        env.update(extra)
    return env


def _private_ca_path(raw: str, *, directory: bool) -> Path | None:
    try:
        path = Path(raw)
        if directory:
            if path.is_dir() and os.access(path, os.R_OK | os.X_OK):
                return path.resolve()
            return None
        if path.is_file() and os.access(path, os.R_OK):
            return path.resolve()
    except (OSError, ValueError):
        return None
    return None


def _container_ca_path(index: int, host_path: Path) -> Path:
    raw_name = host_path.name or "ca"
    safe_name = "".join(
        char if char.isalnum() or char in {".", "-", "_"} else "_"
        for char in raw_name
    )
    return _CONTAINER_CA_ROOT / f"{index:02d}-{safe_name or 'ca'}"


def _container_private_ca(
    env: dict[str, str],
) -> tuple[list[tuple[Path, Path]], dict[str, str]]:
    mounts: list[tuple[Path, Path]] = []
    env_values: dict[str, str] = {}
    host_to_container: dict[Path, Path] = {}

    def add_mapping(key: str, value: str, *, directory: bool) -> None:
        host_path = _private_ca_path(value, directory=directory)
        if host_path is None:
            return
        container_path = host_to_container.get(host_path)
        if container_path is None:
            container_path = _container_ca_path(len(host_to_container), host_path)
            host_to_container[host_path] = container_path
            mounts.append((host_path, container_path))
        env_values[key] = str(container_path)

    for key in _PRIVATE_CA_FILE_ENV_KEYS:
        value = env.get(key, "").strip()
        if value:
            add_mapping(key, value, directory=False)
    for key in _PRIVATE_CA_DIR_ENV_KEYS:
        value = env.get(key, "").strip()
        if value:
            add_mapping(key, value, directory=True)
    return mounts, env_values


def _mcp_tmpfile_from(cmd: list[str]) -> str | None:
    for flag in ("--mcp-config", "--additional-mcp-config"):
        if flag not in cmd:
            continue
        index = cmd.index(flag)
        if index + 1 >= len(cmd):
            continue
        raw_path = cmd[index + 1]
        candidate = raw_path[1:] if raw_path.startswith("@") else raw_path
        if candidate.startswith(tempfile.gettempdir()):
            return candidate
    return None


def _containerized_command(
    cmd: list[str],
    config: AgentConfig,
    session_env: dict[str, str] | None,
    run_env: dict[str, str],
    mcp_tmpfile: str | None,
) -> tuple[list[str], str | None, str | None]:
    plan = containment.active_plan()
    if plan is None or plan.mode != "container" or not plan.engine:
        return cmd, None, None
    container_name = f"codeprobe-agent-{uuid.uuid4().hex}"
    raw_config_dir = (session_env or {}).get("CLAUDE_CONFIG_DIR")
    container_env_keys = set(_CONTAINER_ENV_KEYS)
    if raw_config_dir is None:
        container_env_keys.discard("CLAUDE_CONFIG_DIR")
    private_ca_mounts, private_ca_env_values = _container_private_ca(run_env)
    argv = containerize_argv(
        cmd,
        engine=plan.engine,
        workspace=Path(config.cwd) if config.cwd else Path.cwd(),
        config_dir=Path(raw_config_dir) if raw_config_dir else None,
        mcp_tmpfile=mcp_tmpfile,
        readonly_mounts=private_ca_mounts,
        env_keys=sorted(container_env_keys),
        env_values=private_ca_env_values,
        image=agent_image_reference(),
        name=container_name,
        env=run_env,
    )
    return argv, plan.engine, container_name


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


def _remove_container(engine: str, name: str) -> None:
    """Best-effort ``<engine> rm -f <name>`` after a client-side timeout.

    ``subprocess.run(timeout=...)`` kills the engine client, not the
    container; without this a hung agent container outlives the run.
    Failures are logged, never raised — the caller is already unwinding a
    TimeoutExpired and must return the partial AgentOutput.
    """
    try:
        subprocess.run(  # noqa: S603 — argv list, no shell=True
            [engine, "rm", "-f", name],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        logger.warning("failed to remove timed-out agent container %s", name)


def _signal_agent_process(process: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Signal only the adapter child boundary created for this trial."""
    if process.poll() is not None:
        return
    try:
        if _agent_process_groups_supported():
            os.killpg(process.pid, sig)
        else:
            process.send_signal(sig)
    except ProcessLookupError:
        return
    except OSError:
        logger.warning("failed to signal timed-out agent process", exc_info=True)


def _kill_agent_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    try:
        if _agent_process_groups_supported():
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        return
    except OSError:
        logger.warning("failed to kill timed-out agent process", exc_info=True)


def _terminate_agent_process_tree(
    process: subprocess.Popen[str],
) -> tuple[str | bytes | None, str | bytes | None]:
    _signal_agent_process(process, signal.SIGTERM)
    try:
        return process.communicate(timeout=_AGENT_TERMINATE_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_agent_process(process)
        try:
            return process.communicate(timeout=_AGENT_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired as exc:
            return exc.stdout, exc.stderr


def _run_agent_process(
    cmd: list[str],
    *,
    timeout: int,
    cwd: str | None,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(  # noqa: S603 — argv list, no shell=True
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
        env=env,
        start_new_session=_agent_process_groups_supported(),
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        timeout_stdout, timeout_stderr = _terminate_agent_process_tree(process)
        raise subprocess.TimeoutExpired(
            cmd=cmd,
            timeout=exc.timeout,
            output=timeout_stdout,
            stderr=timeout_stderr,
        ) from exc

    returncode = process.returncode if process.returncode is not None else 0
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
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
        expanded = resolve_mcp_runtime_config(
            config.mcp_config,
            environ=os.environ,
        )
        assert expanded is not None
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
        mcp_tmpfile = _mcp_tmpfile_from(cmd)
        container_engine: str | None = None
        container_name: str | None = None
        start = time.monotonic()
        # _containerized_command must run INSIDE the try: it raises ValueError
        # on an unresolvable image reference and FileNotFoundError from
        # Path.cwd(), and the tempfile it would clean up holds an EXPANDED
        # token. Leaving it outside stranded that secret on disk.
        try:
            run_env = _adapter_safe_env(session_env)
            cmd, container_engine, container_name = _containerized_command(
                cmd, config, session_env, run_env, mcp_tmpfile
            )
            result = _run_agent_process(
                cmd,
                timeout=config.timeout_seconds,
                cwd=config.cwd,
                env=run_env,
            )
        except subprocess.TimeoutExpired as exc:
            return self._timeout_output(
                exc, cmd, config, start, container_engine, container_name
            )
        except FileNotFoundError as exc:
            raise AdapterSetupError(f"Binary not found at runtime: {exc}") from exc
        finally:
            self._cleanup_mcp_tmpfile(mcp_tmpfile)
        duration = time.monotonic() - start
        return self._parse_result(result, duration)

    def _timeout_output(
        self,
        exc: subprocess.TimeoutExpired,
        cmd: list[str],
        config: AgentConfig,
        start: float,
        container_engine: str | None,
        container_name: str | None,
    ) -> AgentOutput:
        duration = time.monotonic() - start
        if container_engine and container_name:
            _remove_container(container_engine, container_name)
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

    def _parse_result(
        self,
        result: subprocess.CompletedProcess[str],
        duration: float,
    ) -> AgentOutput:
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

    def _cleanup_mcp_tmpfile(self, mcp_tmpfile: str | None) -> None:
        if mcp_tmpfile is None:
            return
        try:
            os.unlink(mcp_tmpfile)
        except OSError:
            logger.warning("failed to remove MCP config tempfile")
