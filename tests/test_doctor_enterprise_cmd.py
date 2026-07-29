"""Enterprise-focused doctor command regressions."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import doctor_env, main
from codeprobe.cli.errors import DiagnosticError


class _FakeProc:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def _fake_cli(tmp_path: Path, name: str) -> str:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


def _use_claude_agent_path(monkeypatch: object, tmp_path: Path) -> None:
    import codeprobe.cli.doctor_cmd as mod
    from codeprobe.core import sandbox as codeprobe_sandbox

    claude = _fake_cli(tmp_path, "claude")
    monkeypatch.setattr(
        mod.shutil,
        "which",
        lambda name: claude if name == "claude" else None,
    )
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
    monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    for key in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "OPENAI_API_KEY",
        "GITHUB_TOKEN",
        "COPILOT_API_KEY",
        *doctor_env.PROXY_ENV_KEYS,
        *doctor_env.PRIVATE_CA_FILE_ENV_KEYS,
        *doctor_env.PRIVATE_CA_DIR_ENV_KEYS,
    ):
        monkeypatch.delenv(key, raising=False)


def test_offline_ttl_failure_does_not_leak_fix_message(
    monkeypatch: object, tmp_path: Path
) -> None:
    import importlib

    infra = importlib.import_module("codeprobe.cli.check_infra")
    secret = "secret-ttl-token"

    def _fail_offline_preflight(*args: object, **kwargs: object) -> None:
        raise DiagnosticError(
            code="OFFLINE_PREFLIGHT_FAILED",
            message=f"bedrock credential leaked {secret}",
            diagnose_cmd="codeprobe check-infra offline --json",
            terminal=True,
        )

    _use_claude_agent_path(monkeypatch, tmp_path)
    monkeypatch.setattr(infra, "run_offline_preflight", _fail_offline_preflight)

    runner = CliRunner()
    pretty = runner.invoke(
        main,
        ["doctor", "--agent", "claude", "--offline", "--no-json"],
    )
    json_result = runner.invoke(
        main,
        ["doctor", "--agent", "claude", "--offline", "--json"],
    )

    assert pretty.exit_code == 2
    assert json_result.exit_code == 2
    assert "OFFLINE_PREFLIGHT_FAILED" in pretty.output
    assert "codeprobe check-infra offline --json" in pretty.output
    assert secret not in pretty.output
    assert secret not in json_result.output
