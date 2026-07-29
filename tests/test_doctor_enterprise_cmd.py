"""Enterprise-focused doctor command regressions."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import doctor_env, main
from codeprobe.cli.errors import DiagnosticError
from tests._doctor_helpers import _fake_cli, _FakeProc, _use_claude_agent_path


def _use_containerized_claude_file_auth(monkeypatch: object, tmp_path: Path) -> None:
    import codeprobe.cli.doctor_cmd as mod
    from codeprobe.adapters.claude import ClaudeAdapter
    from codeprobe.core import containment
    from codeprobe.core import sandbox as codeprobe_sandbox
    from codeprobe.sandbox import runner as container_runner

    claude = _fake_cli(tmp_path, "claude")
    monkeypatch.setattr(
        mod.shutil,
        "which",
        lambda name: claude if name == "claude" else None,
    )
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
    monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: False)
    monkeypatch.setattr(container_runner, "detect_engine", lambda: "/usr/bin/docker")
    monkeypatch.setattr(
        container_runner,
        "agent_image_reference",
        lambda: "sha256:" + "a" * 64,
    )
    monkeypatch.setattr(
        container_runner,
        "scoring_image_reference",
        lambda: "sha256:" + "b" * 64,
    )
    monkeypatch.setattr(container_runner, "image_available", lambda *a: True)
    monkeypatch.setattr(
        containment,
        "active_plan",
        lambda: containment.ContainmentPlan(
            mode="container", engine="/usr/bin/docker"
        ),
    )
    fake_home = tmp_path / "home"
    real_claude = fake_home / ".claude"
    real_claude.mkdir(parents=True)
    host_cred = real_claude / ".credentials.json"
    host_cred.write_text('{"token": "doctor-secret"}', encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    for key in (
        "ANTHROPIC_API_KEY",
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

    def _symlinked_session(
        _self: ClaudeAdapter,
        slot_id: int,
        namespace: str | None = None,
        pristine: bool = False,
    ) -> dict[str, str]:
        assert namespace == "doctor-preflight"
        assert pristine is False
        slot = tmp_path / "tmp" / "codeprobe-claude" / f"slot-{slot_id}"
        slot.mkdir(parents=True)
        (slot / ".credentials.json").symlink_to(host_cred)
        return {"CLAUDE_CONFIG_DIR": str(slot)}

    monkeypatch.setattr(ClaudeAdapter, "isolate_session", _symlinked_session)


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


def test_claude_container_auth_failure_has_actionable_remediation(
    monkeypatch: object, tmp_path: Path
) -> None:
    _use_containerized_claude_file_auth(monkeypatch, tmp_path)

    result = CliRunner().invoke(main, ["doctor", "--agent", "claude", "--no-json"])

    assert result.exit_code == 2, result.output
    assert "FAIL  claude auth" in result.output
    assert "host file credentials present" in result.output
    assert "containerized CLAUDE_CONFIG_DIR" in result.output
    assert "symlink" in result.output
    assert "claude login" in result.output
    assert "doctor-secret" not in result.output
