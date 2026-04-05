"""Claude Code agent adapter."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
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


def _normalize_model_for_cli(model: str) -> str:
    """Normalize a model identifier for the Claude CLI.

    Strips date suffixes from full API model IDs so the CLI can resolve them.
    Aliases like 'sonnet' or 'haiku' pass through unchanged.
    """
    return _API_MODEL_DATE_SUFFIX.sub("", model)


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
            cmd.extend(["--mcp-config", mcp_path])

        return cmd

    def isolate_session(self, slot_id: int) -> dict[str, str]:
        """Return a per-slot CLAUDE_CONFIG_DIR for session isolation.

        Copies authentication credentials from the real ``~/.claude/``
        directory so the agent subprocess can authenticate.
        """
        config_dir = (
            Path(tempfile.gettempdir()) / "codeprobe-claude" / f"slot-{slot_id}"
        )
        config_dir.mkdir(parents=True, exist_ok=True)

        # Copy auth credentials from the user's real config dir.
        # Without these the subprocess gets "Not logged in".
        real_config = Path.home() / ".claude"
        if real_config.is_dir():
            for name in ("credentials.json", ".credentials.json"):
                src = real_config / name
                dst = config_dir / name
                if src.is_file() and not dst.exists():
                    shutil.copy2(src, dst)

        return {"CLAUDE_CONFIG_DIR": str(config_dir)}

    def parse_output(
        self, result: subprocess.CompletedProcess[str], duration: float
    ) -> AgentOutput:
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
        )
