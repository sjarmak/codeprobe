"""Claude Code agent adapter."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from codeprobe.adapters._base import BaseAdapter
from codeprobe.adapters.pricing import strip_model_date_suffix
from codeprobe.adapters.protocol import (
    ALLOWED_PERMISSION_MODES,
    AdapterCapabilities,
    AgentConfig,
    AgentOutput,
)
from codeprobe.adapters.quota import detect_quota_error
from codeprobe.adapters.telemetry import (
    JsonStdoutCollector,
    parse_mcp_init_manifest,
)
from codeprobe.core.sandbox import is_sandboxed

# Patterns that indicate an OAuth / API quota was exhausted. Detected
# from raw stdout/stderr because the Claude CLI does not surface these
# as JSON envelopes — it returns a short literal message and exits
# successfully, which would otherwise be scored as a 0.0 task failure
# and silently contaminate the run mean (codeprobe-9xrl).
#
# Robust to wording variants: monthly limits, rate limits, generic
# "quota" terminology. Case-insensitive.
_QUOTA_PATTERN = re.compile(
    r"(?i)"
    r"(monthly\s+usage\s+limit"
    r"|rate\s+limit\s+(?:exceeded|reached)"
    r"|quota\s+(?:exceeded|exhausted)"
    r"|usage\s+limit\s+reached"
    # 2026-06 OAuth wording: "You've hit your session limit · resets 1:10pm".
    # Anchored on "hit your" because bare "session limit" appears in agent
    # prose on session-management tasks and must not halt the run.
    r"|hit\s+your\s+session\s+limit)"
)

_AUTH_FAILURE_PREFIX = "not logged in"


def _cli_origin_text(stdout: str) -> str:
    """Return only the CLI's own literal lines of *stdout*.

    A real OAuth/quota stub is a short literal message that the CLI prints
    *instead of* a JSON envelope ("returns a short literal message and exits
    successfully"). The agent's tool I/O, by contrast, is always emitted as
    ``stream-json`` events — one JSON object per line
    (``type=user/assistant/result/system``). Scanning the whole stdout for
    the quota pattern therefore false-positives whenever the agent merely
    *edits or reasons about* code containing "quota exhausted" / "session
    limit" — which is endemic on the gascity agent-orchestration corpus
    (codeprobe-9tk: one such edit halted a whole run).

    Keeping only the lines that do NOT parse as a JSON object isolates the
    CLI's literal output (the genuine stub) from the agent's structured
    events, so quota detection fires on real exhaustion (codeprobe-9xrl)
    without tripping on agent content.
    """
    literal: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except (ValueError, TypeError):
            literal.append(line)  # not a stream-json event → CLI literal
            continue
        # A bare JSON scalar/string is still CLI literal output, not an
        # agent event (those are always objects with a ``type`` field).
        if not isinstance(parsed, dict) or "type" not in parsed:
            literal.append(line)
    return "\n".join(literal)


def _detect_quota_error(stdout: str, stderr: str | None) -> str | None:
    """Return a normalised quota-error message if either stream matches.

    Scans stderr (where the API/CLI transport surfaces rate-limit messages)
    and the CLI's literal stdout lines only — NOT the agent's stream-json
    tool I/O (see :func:`_cli_origin_text`). Delegates the pattern match to
    the shared :mod:`codeprobe.adapters.quota` detector, which returns the
    triggering line so the executor can include it in the task's error
    metadata.
    """
    return detect_quota_error(_cli_origin_text(stdout), stderr)


def _detect_auth_failure(stdout: str, stderr: str | None) -> str | None:
    """Return Claude CLI's literal unauthenticated-session message.

    The stable ``Not logged in`` prefix is emitted by the CLI itself. Restrict
    stdout matching to non-JSON CLI lines so agent-authored content cannot
    classify a run as an authentication failure.
    """
    for stream in (_cli_origin_text(stdout), stderr or ""):
        for line in stream.splitlines():
            message = line.strip()
            if message.casefold().startswith(_AUTH_FAILURE_PREFIX):
                return message
    return None


# Result-record subtypes that mark a TERMINAL agent outcome — the CLI ran
# the agent to a protocol-defined stop condition, so a 0.0 reward is a
# genuine measurement (kept on checkpoint resume), not an infra casualty
# (retried). Deliberately conservative: misclassifying an infra casualty
# as terminal banks a bogus 0.0 forever, while misclassifying a terminal
# failure as infra merely re-runs it (codeprobe-8up). Structural protocol
# classification over verbatim CLI enum values, not semantic judgment.
_TERMINAL_RESULT_SUBTYPES = frozenset({"error_max_turns"})

# Credential files whose presence marks a file-based login.  Used by
# ``isolate_session`` to decide whether to mirror ~/.claude per slot.
_FILE_CRED_NAMES: tuple[str, ...] = ("credentials.json", ".credentials.json")

# Per-session mutable state that must NOT be shared across parallel slots.
# Each slot gets a fresh empty directory or empty file for these names so
# concurrent workers never race on session-env writes, history rotations,
# or project-trust state — previously the shared-state racing produced
# intermittent API 401 errors (codeprobe-nac).
_MUTABLE_DIR_NAMES: frozenset[str] = frozenset(
    {
        "session-env",
        "sessions",
        "shell-snapshots",
        "projects",
        "file-history",
        "paste-cache",
        "statsig",
        "logs",
        "tasks",
        "telemetry",
        "backups",
        "cache",
    }
)
_MUTABLE_FILE_NAMES: frozenset[str] = frozenset({"history.jsonl"})

# Operator personalization that biases eval arms when mirrored into a
# slot config dir (global memory, settings, skills, hooks, ...). Excluded
# from the mirror when ``pristine=True`` so two operators produce the
# same effective agent config on the same repo. Credential files and
# ``.claude.json`` (auth + project-trust state) stay mirrored; the
# user-level MCP servers inside ``.claude.json`` are neutralized by the
# unconditional ``--strict-mcp-config`` in ``build_command``.
_PERSONALIZATION_NAMES: frozenset[str] = frozenset(
    {
        "CLAUDE.md",
        "settings.json",
        "settings.local.json",
        "skills",
        "agents",
        "hooks",
        "plugins",
        "commands",
        "rules",
    }
)
_SAFE_NAMESPACE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_CREDENTIAL_FILE_MODE = 0o600

# The built-in that loads deferred tool schemas. Claude Code does not offer
# MCP tools in the init surface: it advertises them by name and the model
# must call ``ToolSearch`` to load a schema before the tool is invocable.
# Restricting ``--tools`` to a list that omits it therefore removes every
# MCP tool as collateral damage — see ``_builtin_tools_for``.
_TOOL_SEARCH = "ToolSearch"


def _normalize_model_for_cli(model: str) -> str:
    """Normalize a model identifier for the Claude CLI.

    The CLI accepts aliases (sonnet, opus, haiku) or short model IDs
    (claude-sonnet-4-6) but NOT full API model IDs with date suffixes
    (claude-sonnet-4-6-20250514) — strip the suffix when present.
    Aliases like 'sonnet' or 'haiku' pass through unchanged.
    """
    return strip_model_date_suffix(model)


def _effective_claude_config_dir() -> Path:
    """Return the directory the Claude CLI actually uses for credentials.

    Respects the ``CLAUDE_CONFIG_DIR`` env var (Claude Code's own convention
    for switching between accounts / sandboxed configs); falls back to
    ``~/.claude``. Without this, codeprobe would check the default location
    even when the user has an account-specific config elsewhere and miss
    their real (refreshed) credentials.
    """
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude"


def _credentials_file_status(config_dir: Path) -> str:
    """Return the status of the credentials file in ``config_dir``.

    Returns one of:

    * ``"missing"`` — no recognized credentials file exists.
    * ``"expired"`` — a credentials file exists but the OAuth token's
      ``expiresAt`` timestamp is in the past.
    * ``"invalid"`` — a credentials file cannot be read as JSON.
    * ``"valid"`` — a credentials file exists and either has no expiry
      info or has not yet expired.

    ``"valid"`` is the default when parsed JSON has an unknown shape because
    non-OAuth credential formats are supported. Malformed or unreadable JSON
    fails closed so doctor cannot report unusable credentials as ready.
    """
    for name in _FILE_CRED_NAMES:
        path = config_dir / name
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "invalid"
        oauth = raw.get("claudeAiOauth") if isinstance(raw, dict) else None
        if not isinstance(oauth, dict):
            return "valid"
        expires_at_ms = oauth.get("expiresAt")
        if not isinstance(expires_at_ms, (int, float)):
            return "valid"
        return "expired" if (expires_at_ms / 1000.0) <= time.time() else "valid"
    return "missing"


def _sanitize_namespace(namespace: str | None) -> str | None:
    """Return a filesystem-safe namespace component for temp dirs."""
    if not namespace:
        return None
    cleaned = _SAFE_NAMESPACE_CHARS.sub("-", namespace).strip(".-")
    return cleaned or None


def _remove_slot_entry(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _materialize_credential_file(source: Path, target: Path) -> None:
    """Copy a credential into a private container-writable session file."""
    if target.exists() or target.is_symlink():
        _remove_slot_entry(target)

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    target_fd = -1
    try:
        target_fd = os.open(target, flags, _CREDENTIAL_FILE_MODE)
        with source.open("rb") as source_file, os.fdopen(
            target_fd, "wb"
        ) as target_file:
            target_fd = -1
            os.fchmod(target_file.fileno(), _CREDENTIAL_FILE_MODE)
            shutil.copyfileobj(source_file, target_file)
            target_file.flush()
            descriptor_stat = os.fstat(target_file.fileno())
            path_stat = os.stat(target, follow_symlinks=False)
            if (descriptor_stat.st_dev, descriptor_stat.st_ino) != (
                path_stat.st_dev,
                path_stat.st_ino,
            ):
                raise OSError("Credential slot path changed during copy")
    except OSError:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    finally:
        if target_fd >= 0:
            os.close(target_fd)


def _build_mirror_slot_env(
    real_config: Path,
    slot_id: int,
    namespace: str | None = None,
    pristine: bool = False,
) -> dict[str, str]:
    """Build a per-slot ``CLAUDE_CONFIG_DIR`` that mirrors ``real_config``.

    Credential files are copied into private regular files in the
    container-mounted slot dir. They never share an inode with live host
    credentials, so an agent-side refresh cannot mutate or replace global
    state through the read-write mount. Other read-mostly entries
    (settings.json, skills/, agents/, hooks/, plugins/, commands/, rules/) are
    symlinked to the live source so host execution keeps current configuration.
    Mutable per-session state (``_MUTABLE_DIR_NAMES`` and
    ``_MUTABLE_FILE_NAMES``) is recreated as fresh empty dirs/files inside the
    slot to prevent parallel-worker races.

    When ``pristine`` is True, ``_PERSONALIZATION_NAMES`` entries are not
    mirrored — and are deliberately kept out of ``seen`` so the stale-entry
    sweep below purges leftovers from a slot dir previously built
    non-pristine. Credentials and ``.claude.json`` remain mirrored.

    Stale symlinks from earlier isolation runs are refreshed so that
    additions, removals, or changes in ``real_config`` propagate to every
    slot.  Existing slot-local mutable dirs are preserved between tasks
    running in the same slot so intra-slot session continuity is not
    broken.
    """
    slot_root = Path(tempfile.gettempdir()) / "codeprobe-claude"
    safe_namespace = _sanitize_namespace(namespace)
    if safe_namespace:
        slot_root = slot_root / safe_namespace
    slot_dir = slot_root / f"slot-{slot_id}"
    slot_dir.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    for entry in real_config.iterdir():
        if pristine and entry.name in _PERSONALIZATION_NAMES:
            continue
        seen.add(entry.name)
        target = slot_dir / entry.name
        is_mutable = entry.name in _MUTABLE_DIR_NAMES or entry.name in _MUTABLE_FILE_NAMES

        if entry.name in _FILE_CRED_NAMES and entry.is_file():
            _materialize_credential_file(entry, target)
            continue

        if is_mutable:
            # Preserve existing slot-local state so tasks within the same
            # slot can keep their own session history; only seed missing
            # entries so fresh slots start clean.
            if target.exists() and not target.is_symlink():
                continue
            if target.is_symlink():
                target.unlink()
            if entry.name in _MUTABLE_DIR_NAMES:
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.touch()
            continue

        if target.is_symlink() or target.exists():
            _remove_slot_entry(target)

        try:
            target.symlink_to(entry)
        except OSError:
            if entry.is_dir():
                shutil.copytree(entry, target, symlinks=True)
            else:
                shutil.copy2(entry, target)

    # Drop stale mirror entries whose source has been removed from the
    # real config dir (so the slot dir doesn't accumulate broken links
    # across runs).
    for stale in slot_dir.iterdir():
        if stale.name in seen:
            continue
        if stale.is_symlink() or not stale.is_dir():
            try:
                stale.unlink()
            except OSError:
                pass

    return {"CLAUDE_CONFIG_DIR": str(slot_dir)}


class ClaudeAdapter(BaseAdapter):
    """Adapter for Claude Code CLI (claude -p)."""

    _binary_name = "claude"
    _install_hint = "Claude CLI not found. Install from https://claude.ai/download"

    @property
    def capabilities(self) -> AdapterCapabilities:
        # Grep-verified: build_command consumes mcp_config, allowed_tools,
        # disallowed_tools, max_turns, and permission_mode; BaseAdapter.run
        # enforces cwd and timeout_seconds on the subprocess.
        return AdapterCapabilities(
            mcp_config=True,
            allowed_tools=True,
            disallowed_tools=True,
            max_turns=True,
            permission_mode=True,
            workspace_cwd=True,
            timeout=True,
        )

    def __init__(self) -> None:
        self._collector = JsonStdoutCollector()
        # Thread-local trace context: per-worker TraceRecorder + task_id so
        # parallel task threads don't collide on a single shared attribute.
        # The executor sets this in ``_run_one`` before calling ``run()`` and
        # clears it afterwards.
        self._trace_ctx: threading.local = threading.local()

    def set_trace_context(
        self,
        *,
        recorder: Any | None,
        config: str | None,
        task_id: str | None,
    ) -> None:
        """Bind trace-recorder state for the current thread.

        Called by the executor before running a task. ``parse_output``
        forwards these keys to ``JsonStdoutCollector.collect(**ctx)`` so
        R5's trace.db is populated at the same parse step that fills
        ``UsageData``. Passing ``recorder=None`` clears the context.
        """
        self._trace_ctx.recorder = recorder
        self._trace_ctx.config = config
        self._trace_ctx.task_id = task_id

    def _current_trace_context(self) -> dict[str, Any]:
        """Return kwargs for ``collect()`` from the thread-local trace slot."""
        recorder = getattr(self._trace_ctx, "recorder", None)
        config = getattr(self._trace_ctx, "config", None)
        task_id = getattr(self._trace_ctx, "task_id", None)
        if recorder is None or config is None or task_id is None:
            return {}
        return {
            "trace_recorder": recorder,
            "trace_config": config,
            "trace_task_id": task_id,
        }

    def preflight(self, config: AgentConfig) -> list[str]:
        issues = super().preflight(config)
        if config.permission_mode == "dangerously_skip" and not is_sandboxed():
            issues.append(
                "permission_mode='dangerously_skip' requires a sandboxed environment "
                "(container); run inside a container, or pass --uncontained to "
                "'codeprobe run' to accept host execution"
            )
        return issues

    @staticmethod
    def check_parallel_auth(parallel: int) -> str | None:
        """Return a warning message when parallel execution cannot be isolated.

        Session isolation via per-slot ``CLAUDE_CONFIG_DIR`` requires
        either a file-based credential in ``~/.claude/`` or an explicit
        env-var (``ANTHROPIC_API_KEY`` / ``CLAUDE_CODE_OAUTH_TOKEN``).
        When none of those are present and ``parallel > 1``, workers
        share the real ``~/.claude`` state and can race on session-env
        writes / OAuth refreshes — observed in the wild as every
        parallel task hitting API 401 (codeprobe-nac).

        Returns ``None`` when parallel is safe; otherwise a user-facing
        string describing the issue and the recommended remediation.
        """
        if parallel <= 1:
            return None

        config_dir = _effective_claude_config_dir()
        creds_status = _credentials_file_status(config_dir)
        has_env_auth = bool(
            os.environ.get("ANTHROPIC_API_KEY")
            or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        )

        if creds_status == "valid" or has_env_auth:
            return None

        if creds_status == "expired":
            return (
                f"Claude CLI credentials at {config_dir} are EXPIRED. "
                "Every agent run will fail with API 401 until refreshed. "
                "Run `claude login` to renew the OAuth token, or export "
                "ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN."
            )

        return (
            f"Claude CLI has no file-based credentials in {config_dir} and "
            "no ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN env var — "
            "parallel execution cannot isolate session state and may hit "
            "API 401 errors (codeprobe-nac). Re-run with --parallel 1, or "
            "sign in with `claude login`, or export ANTHROPIC_API_KEY / "
            "CLAUDE_CODE_OAUTH_TOKEN."
        )

    def build_command(self, prompt: str, config: AgentConfig) -> list[str]:
        binary = self._require_binary()
        # stream-json + --verbose emits newline-delimited events including
        # every assistant message (with tool_use content blocks) and ends
        # with a ``type: "result"`` event mirroring the ``json`` envelope.
        # This is what gives us accurate per-run tool_call_count and
        # per-tool observability; the collector reconstructs the envelope
        # from the terminal event.
        cmd = [binary, "-p", prompt, "--output-format", "stream-json", "--verbose"]

        if config.model:
            cmd.extend(["--model", _normalize_model_for_cli(config.model)])

        # Hard cap on agent turns. ``None`` means uncapped (historical
        # default); when set, codeprobe matches the rig surface used by CSB
        # (30) and EB (50). Acts as a backstop against runaway loops where
        # the only other limit is the per-task subprocess timeout.
        if config.max_turns is not None:
            if not isinstance(config.max_turns, int) or config.max_turns <= 0:
                raise ValueError(
                    f"max_turns must be a positive integer, got {config.max_turns!r}"
                )
            cmd.extend(["--max-turns", str(config.max_turns)])

        if config.permission_mode == "dangerously_skip":
            cmd.append("--dangerously-skip-permissions")
        elif config.permission_mode != "default":
            if config.permission_mode not in ALLOWED_PERMISSION_MODES:
                raise ValueError(
                    f"Unsafe permission_mode: {config.permission_mode!r}. "
                    f"Allowed: {', '.join(sorted(ALLOWED_PERMISSION_MODES))}"
                )
            cmd.extend(["--permission-mode", config.permission_mode])

        # Pin the MCP tool surface unconditionally. Without a bare
        # --strict-mcp-config, an arm that declares no mcp_config silently
        # inherits the operator's ambient MCP servers (user-level
        # ~/.claude.json and repo-level .mcp.json), so an "MCP off"
        # baseline is not actually off and two operators get different
        # numbers on the same repo. With the flag, only servers named via
        # --mcp-config are loaded — none when the flag stands alone.
        mcp_path = self._write_mcp_config(config)
        if mcp_path:
            cmd.extend(["--mcp-config", mcp_path])
        cmd.append("--strict-mcp-config")

        # Tool restrictions. Claude CLI has three related flags:
        #   --tools A,B           restricts the *built-in* tool allowlist
        #                         to these names; ``--tools ""`` disables
        #                         all built-ins. MCP tools come from
        #                         ``mcp_config`` and are NOT valid entries
        #                         here.
        #   --allowedTools X,Y    auto-approves these tools (no permission
        #                         prompt); names may include MCP tools as
        #                         ``mcp__<server>__<tool>``.
        #   --disallowedTools X,Y blocks these tools outright.
        #
        # We treat ``allowed_tools`` as a whitelist. Regression (r7): prior
        # to this fix the adapter passed ``--tools ""`` unconditionally,
        # which stripped every built-in including listed ones like
        # ``Write`` — so a whitelist like
        # ``["Write", "mcp__sourcegraph__keyword_search"]`` silently lost
        # ``Write``. The agent then could not persist ``answer.json`` for
        # structured-retrieval tasks and scoring fell back to the
        # ``$AGENT_OUTPUT`` stdout transcript. ``--allowedTools`` only
        # auto-approves; it does not re-enable a stripped built-in.
        #
        # Fix: partition ``allowed_tools`` into built-in vs MCP names
        # (``mcp__<server>__<tool>`` is the canonical MCP prefix). Pass
        # the built-in subset to ``--tools`` so listed built-ins stay
        # available while unlisted built-ins are still disabled. Pass the
        # full list to ``--allowedTools`` so both built-ins and MCP calls
        # are auto-approved.
        #
        # Second regression (r8): ``--tools`` also gates the MCP surface.
        # Claude Code advertises MCP tools by name but defers their schemas;
        # the model must call ``ToolSearch`` to load one before it can be
        # invoked. A whitelist like ``["Write"]`` strips ``ToolSearch``, and
        # with it every ``mcp__<server>__<tool>`` disappears from the init
        # surface entirely — so a strict MCP arm silently measured an agent
        # with NO working tools rather than an agent using MCP. Re-add
        # ``ToolSearch`` whenever MCP servers are configured.
        # A name in both lists is a contradiction the CLI resolves silently in
        # favour of --disallowedTools, so the arm runs without a tool its own
        # config and report claim it had. Refuse rather than mis-report.
        if config.allowed_tools and config.disallowed_tools:
            conflict = sorted(set(config.allowed_tools) & set(config.disallowed_tools))
            if conflict:
                raise ValueError(
                    "allowed_tools and disallowed_tools both list "
                    f"{', '.join(conflict)}; --disallowedTools wins silently, so "
                    "the arm would not have the tools its config claims."
                )

        disallowed = list(config.disallowed_tools or [])
        if mcp_path and _TOOL_SEARCH in disallowed:
            # Blocking the schema loader disables every MCP tool, which turns
            # an MCP arm into a no-tools arm that still reports plausible
            # scores. Refuse instead of measuring nothing.
            raise ValueError(
                f"disallowed_tools blocks {_TOOL_SEARCH}, which loads MCP tool "
                "schemas; with an mcp_config set this leaves the agent unable "
                "to call any MCP tool."
            )

        if config.allowed_tools is not None:
            builtin_tools = [
                t for t in config.allowed_tools if not t.startswith("mcp__")
            ]
            auto_approved = list(config.allowed_tools)
            if mcp_path and _TOOL_SEARCH not in builtin_tools:
                builtin_tools.append(_TOOL_SEARCH)
                auto_approved.append(_TOOL_SEARCH)
            cmd.extend(["--tools", ",".join(builtin_tools)])
            if auto_approved:
                cmd.extend(["--allowedTools", ",".join(auto_approved)])
        if disallowed:
            cmd.extend(["--disallowedTools", ",".join(disallowed)])

        return cmd

    def isolate_session(
        self,
        slot_id: int,
        namespace: str | None = None,
        pristine: bool = False,
    ) -> dict[str, str]:
        """Return a per-slot ``CLAUDE_CONFIG_DIR`` for session isolation.

        Mirrors the real Claude config directory (honoring the
        ``CLAUDE_CONFIG_DIR`` env var, so account-specific configs are
        respected) into a slot-specific temp dir via symlinks, with fresh
        empty directories for mutable per-session state (``session-env/``,
        ``sessions/``, ``history.jsonl``, etc.). Credential files are copied
        into private regular files in the slot, so agent containers can read
        the exact mounted path without gaining a write path to host credential
        state. Fresh mutable subdirs prevent parallel workers from racing on
        shared state — which under real load manifested as API 401 errors
        (codeprobe-nac).

        When ``pristine`` is True, operator personalization
        (``_PERSONALIZATION_NAMES``: CLAUDE.md, settings, skills, agents,
        hooks, plugins, commands, rules) is excluded from the mirror so
        eval arms are reproducible across operators.

        When no credential file is found the CLI is presumed to use the OS
        keychain; in that case this returns an empty dict so the agent
        uses the default config dir and keychain reads continue to work.
        Callers should combine this with a preflight warning for the
        ``parallel > 1 + no-file-creds`` combination.
        """
        real_config = _effective_claude_config_dir()
        if any((real_config / name).is_file() for name in _FILE_CRED_NAMES):
            return _build_mirror_slot_env(
                real_config,
                slot_id,
                namespace=namespace,
                pristine=pristine,
            )

        return {}

    def cleanup_session_namespace(self, namespace: str | None) -> None:
        """Remove a namespaced temp CLAUDE_CONFIG_DIR tree after a run."""
        safe_namespace = _sanitize_namespace(namespace)
        if not safe_namespace:
            return
        slot_root = Path(tempfile.gettempdir()) / "codeprobe-claude" / safe_namespace
        try:
            shutil.rmtree(slot_root)
        except FileNotFoundError:
            return
        except OSError:
            pass

    def parse_output(self, result: subprocess.CompletedProcess[str], duration: float) -> AgentOutput:
        """Parse Claude CLI JSON envelope into AgentOutput.

        Handles both ``--output-format json`` (single envelope) and
        ``--output-format stream-json --verbose`` (newline-delimited
        events) — the collector auto-detects. When parsing a stream, the
        final ``type: "result"`` event carries the same fields as the
        single-envelope shape, so we reconstruct ``result`` text from it.
        """
        usage = self._collector.collect(
            result.stdout, **self._current_trace_context()
        )

        # Zero-inference proof of the offered tool surface (codeprobe-9p6).
        # Parsed from the stream-json init event; a captured-but-empty or
        # failed-attach manifest is recorded explicitly, never dropped.
        mcp_init = parse_mcp_init_manifest(result.stdout)

        # Extract content text. For stream-json, the terminal result event
        # has a ``result`` field; iterate events to find it. For single
        # envelope, json.loads works directly.
        stdout_text = result.stdout
        try:
            envelope = json.loads(result.stdout)
            stdout_text = envelope.get("result", result.stdout)
        except (json.JSONDecodeError, ValueError):
            for line in reversed(result.stdout.splitlines()):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(ev, dict) and ev.get("type") == "result":
                    stdout_text = ev.get("result", result.stdout)
                    break

        # Transport-error detection runs against CLI-origin stdout/stderr.
        # A structured no-work error envelope may carry the same transport
        # message in ``usage.error``; token-bearing envelopes are excluded
        # because their result text can be agent-authored.
        auth_message = _detect_auth_failure(result.stdout, result.stderr)
        ran_work = bool((usage.input_tokens or 0) + (usage.output_tokens or 0))
        if auth_message is None and not ran_work:
            auth_message = _detect_auth_failure("", usage.error)
        quota_message = _detect_quota_error(result.stdout, result.stderr)
        error_text: str | None
        error_category: str | None
        if auth_message is not None:
            error_text = auth_message
            error_category = "auth_failure"
        elif quota_message is not None:
            error_text = f"OAuth quota exhausted: {quota_message}"
            error_category = "quota"
        else:
            error_text = usage.error
            error_category = None

        # Quota wins over subtype: a quota stub is an infra casualty even
        # when the CLI also produced a result envelope.
        error_terminal = (
            auth_message is None
            and quota_message is None
            and usage.result_subtype in _TERMINAL_RESULT_SUBTYPES
        )

        return AgentOutput(
            stdout=stdout_text,
            stderr=result.stderr or None,
            exit_code=result.returncode,
            duration_seconds=duration,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_creation_tokens=usage.cache_creation_tokens,
            cost_usd=usage.cost_usd,
            cost_model=usage.cost_model,
            cost_source=usage.cost_source,
            error=error_text,
            error_category=error_category,
            error_terminal=error_terminal,
            tool_call_count=usage.tool_call_count,
            tool_use_by_name=usage.tool_use_by_name,
            num_turns=usage.num_turns,
            result_subtype=usage.result_subtype,
            duration_api_ms=usage.duration_api_ms,
            mcp_init=mcp_init,
        )
