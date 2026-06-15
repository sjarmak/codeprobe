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
from codeprobe.adapters.protocol import (
    ALLOWED_PERMISSION_MODES,
    AgentConfig,
    AgentOutput,
)
from codeprobe.adapters.telemetry import (
    JsonStdoutCollector,
    parse_mcp_init_manifest,
)
from codeprobe.core.sandbox import is_sandboxed

# Claude CLI accepts aliases (sonnet, opus, haiku) or short model IDs
# (claude-sonnet-4-6) but NOT full API model IDs with date suffixes
# (claude-sonnet-4-6-20250514). Strip the date suffix when present.
_API_MODEL_DATE_SUFFIX = re.compile(r"(-\d{8})$")

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


def _detect_quota_error(stdout: str, stderr: str | None) -> str | None:
    """Return a normalised quota-error message if either stream matches.

    The match looks at both stdout (where Claude CLI writes the OAuth
    "monthly usage limit" stub) and stderr (where API/CLI surfaces
    rate-limit messages from the underlying transport). Returns the
    triggering line so the executor can include it in the task's
    error metadata.
    """
    for stream in (stdout, stderr or ""):
        if not stream:
            continue
        match = _QUOTA_PATTERN.search(stream)
        if match:
            # Find and return the line containing the match so the user
            # sees the exact wording (helps Anthropic message rewording).
            for line in stream.splitlines():
                if _QUOTA_PATTERN.search(line):
                    return line.strip()
            return match.group(0)
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
_SAFE_NAMESPACE_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _normalize_model_for_cli(model: str) -> str:
    """Normalize a model identifier for the Claude CLI.

    Strips date suffixes from full API model IDs so the CLI can resolve them.
    Aliases like 'sonnet' or 'haiku' pass through unchanged.
    """
    return _API_MODEL_DATE_SUFFIX.sub("", model)


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
    * ``"valid"`` — a credentials file exists and either has no expiry
      info or has not yet expired.

    ``"valid"`` is the default when the file is present but its shape
    is unknown (non-OAuth formats, unreadable JSON): we trust the CLI to
    handle those cases and let it surface any auth errors natively.
    """
    for name in _FILE_CRED_NAMES:
        path = config_dir / name
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "valid"
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


def _build_mirror_slot_env(
    real_config: Path,
    slot_id: int,
    namespace: str | None = None,
) -> dict[str, str]:
    """Build a per-slot ``CLAUDE_CONFIG_DIR`` that mirrors ``real_config``.

    Read-mostly entries (credentials file, settings.json, skills/, agents/,
    hooks/, plugins/, commands/, rules/) are symlinked to the live source
    so configuration and OAuth-refreshed credentials stay coherent across
    slots.  Mutable per-session state (``_MUTABLE_DIR_NAMES`` and
    ``_MUTABLE_FILE_NAMES``) is recreated as fresh empty dirs/files inside
    the slot to prevent parallel-worker races.

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
        seen.add(entry.name)
        target = slot_dir / entry.name
        is_mutable = entry.name in _MUTABLE_DIR_NAMES or entry.name in _MUTABLE_FILE_NAMES

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
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()

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
                "(Docker container or CODEPROBE_SANDBOX=1)"
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

        mcp_path = self._write_mcp_config(config)
        if mcp_path:
            cmd.extend(["--mcp-config", mcp_path, "--strict-mcp-config"])

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
        if config.allowed_tools is not None:
            builtin_tools = [
                t for t in config.allowed_tools if not t.startswith("mcp__")
            ]
            cmd.extend(["--tools", ",".join(builtin_tools)])
            if config.allowed_tools:
                cmd.extend(["--allowedTools", ",".join(config.allowed_tools)])
        if config.disallowed_tools:
            cmd.extend(
                ["--disallowedTools", ",".join(config.disallowed_tools)]
            )

        return cmd

    def isolate_session(
        self,
        slot_id: int,
        namespace: str | None = None,
    ) -> dict[str, str]:
        """Return a per-slot ``CLAUDE_CONFIG_DIR`` for session isolation.

        Mirrors the real Claude config directory (honoring the
        ``CLAUDE_CONFIG_DIR`` env var, so account-specific configs are
        respected) into a slot-specific temp dir via symlinks, with fresh
        empty directories for mutable per-session state (``session-env/``,
        ``sessions/``, ``history.jsonl``, etc.). Symlinking the credentials
        file keeps OAuth-refresh coherence across slots (all workers see
        the same live creds) while the fresh mutable subdirs prevent
        parallel workers from racing on shared state — which under real
        load manifested as API 401 errors (codeprobe-nac).

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

        # Quota detection runs against the raw stdout / stderr (not the
        # extracted ``stdout_text``) because the OAuth "monthly usage
        # limit" stub is the entire response — there is no JSON envelope
        # to peel back. ``usage.error`` may be empty for the same reason
        # (no envelope means no error field), so we explicitly synthesise
        # one when quota is detected (codeprobe-9xrl).
        quota_message = _detect_quota_error(result.stdout, result.stderr)
        if quota_message is not None:
            error_text = f"OAuth quota exhausted: {quota_message}"
            error_category: str | None = "quota"
        else:
            error_text = usage.error
            error_category = None

        # Quota wins over subtype: a quota stub is an infra casualty even
        # when the CLI also produced a result envelope.
        error_terminal = (
            quota_message is None
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
