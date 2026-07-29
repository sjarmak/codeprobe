"""Shared helpers for doctor command tests."""

from __future__ import annotations

import json
from pathlib import Path


class _FakeProc:
    """Minimal subprocess.CompletedProcess stand-in for mocked runs."""

    def __init__(self, returncode: int, *, stdout: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = ""


def _fake_cli(tmp_path: Path, name: str, *, returncode: int = 0) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\nexit {returncode}\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _use_tool_paths(
    monkeypatch: object, tmp_path: Path, names: tuple[str, ...]
) -> None:
    import codeprobe.cli.doctor_cmd as mod

    paths = {name: _fake_cli(tmp_path, name) for name in names}
    monkeypatch.setattr(mod.shutil, "which", lambda name: paths.get(name))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def _use_claude_agent_path(
    monkeypatch: object, tmp_path: Path, *, authenticated: bool = True
) -> None:
    import codeprobe.cli.doctor_cmd as mod
    from codeprobe.cli import doctor_env
    from codeprobe.core import sandbox as codeprobe_sandbox

    _use_tool_paths(monkeypatch, tmp_path, ("claude",))
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
    monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path))
    if authenticated:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    for key in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "OPENAI_API_KEY",
        "COPILOT_GITHUB_TOKEN",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "COPILOT_API_KEY",
        "COPILOT_OFFLINE",
        "COPILOT_PROVIDER_BASE_URL",
        "COPILOT_PROVIDER_API_KEY",
        "COPILOT_PROVIDER_TYPE",
        "COPILOT_MODEL",
        *doctor_env.PROXY_ENV_KEYS,
        *doctor_env.PRIVATE_CA_FILE_ENV_KEYS,
        *doctor_env.PRIVATE_CA_DIR_ENV_KEYS,
    ):
        monkeypatch.delenv(key, raising=False)


def _last_json_payload(output: str) -> dict[str, object]:
    return json.loads(output.strip().splitlines()[-1])
