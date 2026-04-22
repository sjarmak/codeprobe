"""Claude Code agent adapter."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from codeprobe.adapters._base import BaseAdapter
from codeprobe.adapters.protocol import (
    ALLOWED_PERMISSION_MODES,
    AgentConfig,
    AgentOutput,
)
from codeprobe.adapters.telemetry import JsonStdoutCollector
from codeprobe.core.sandbox import is_sandboxed

# Claude CLI accepts aliases (sonnet, opus, haiku) or short model IDs
# (claude-sonnet-4-6) but NOT full API model IDs with date suffixes
# (claude-sonnet-4-6-20250514). Strip the date suffix when present.
_API_MODEL_DATE_SUFFIX = re.compile(r"(-\d{8})$")

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


def _build_mirror_slot_env(real_config: Path, slot_id: int) -> dict[str, str]:
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
    slot_dir = Path(tempfile.gettempdir()) / "codeprobe-claude" / f"slot-{slot_id}"
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
        cmd = [binary, "-p", prompt, "--output-format", "json"]

        if config.model:
            cmd.extend(["--model", _normalize_model_for_cli(config.model)])

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

        return cmd

    def isolate_session(self, slot_id: int) -> dict[str, str]:
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
            return _build_mirror_slot_env(real_config, slot_id)

        return {}

    def parse_output(self, result: subprocess.CompletedProcess[str], duration: float) -> AgentOutput:
        """Parse Claude CLI JSON envelope into AgentOutput."""
        usage = self._collector.collect(result.stdout)

        # Extract content text from the JSON envelope
        try:
            envelope = json.loads(result.stdout)
            stdout_text = envelope.get("result", result.stdout)
        except (json.JSONDecodeError, ValueError):
            stdout_text = result.stdout

        return AgentOutput(
            stdout=stdout_text,
            stderr=result.stderr or None,
            exit_code=result.returncode,
            duration_seconds=duration,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cost_usd=usage.cost_usd,
            cost_model=usage.cost_model,
            cost_source=usage.cost_source,
            error=usage.error,
            tool_call_count=usage.tool_call_count,
        )
