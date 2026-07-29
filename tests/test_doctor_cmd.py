"""CLI rendering and system-boundary tests for the doctor command."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.cli.doctor_cmd import (
    run_checks,
)
from tests._doctor_helpers import (
    _fake_cli,
    _FakeProc,
    _last_json_payload,
    _use_claude_agent_path,
    _use_tool_paths,
)


class TestDoctorCLI:
    """Integration tests for the CLI command."""

    def test_selected_agent_ignores_absent_unselected_agents(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        _use_claude_agent_path(monkeypatch, tmp_path)

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])

        assert result.exit_code == 0, result.output
        assert "PASS  claude CLI" in result.output
        assert "INFO  copilot CLI" in result.output
        assert "FAIL  copilot CLI" not in result.output

    def test_selected_agent_renders_usable_unselected_agents_as_info(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        from codeprobe.core import sandbox as codeprobe_sandbox

        _use_tool_paths(monkeypatch, tmp_path, ("claude", "copilot", "gh"))
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: True)
        monkeypatch.setattr(
            "codeprobe.cli.doctor_cmd.subprocess.run",
            lambda *a, **k: _FakeProc(0),
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "github_pat_test")
        monkeypatch.setenv("HOME", str(tmp_path))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])

        assert result.exit_code == 0, result.output
        assert "PASS  claude CLI" in result.output
        assert "INFO  copilot CLI (found)" in result.output
        assert "INFO  copilot auth (COPILOT_GITHUB_TOKEN set)" in result.output
        assert "PASS  copilot CLI" not in result.output
        assert "PASS  copilot auth" not in result.output

    def test_doctor_auto_selects_one_usable_agent(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        _use_claude_agent_path(monkeypatch, tmp_path)

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--no-json"])

        assert result.exit_code == 0, result.output
        assert "PASS  selected agent (claude auto-selected)" in result.output
        assert "FAIL  copilot CLI" not in result.output

    def test_selected_agent_requires_auth(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        _use_claude_agent_path(monkeypatch, tmp_path, authenticated=False)

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])

        assert result.exit_code == 2
        assert "FAIL  claude auth" in result.output
        assert str(tmp_path) not in result.output

    def test_unusable_selected_copilot_fails_compact_and_full_json(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod
        from codeprobe.core import sandbox as codeprobe_sandbox
        from codeprobe.sandbox import runner as container_runner

        false_cli = _fake_cli(tmp_path, "copilot", returncode=1)
        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: str(false_cli) if name == "copilot" else None,
        )
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: False)
        monkeypatch.setattr(container_runner, "detect_engine", lambda: None)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("COPILOT_API_KEY", "obsolete-secret")
        for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        runner = CliRunner()
        compact = runner.invoke(
            main, ["doctor", "--agent", "copilot", "--json", "--compact"]
        )
        full = runner.invoke(main, ["doctor", "--agent", "copilot", "--json"])

        compact_payload = _last_json_payload(compact.output)
        full_payload = _last_json_payload(full.output)
        subsystem = {
            row["name"]: row for row in full_payload["data"]["subsystem_status"]
        }

        assert compact.exit_code == 1, compact.output
        assert compact_payload["ok"] is False
        assert compact_payload["data"]["llm_available"] is False
        assert full.exit_code == 2, full.output
        assert full_payload["ok"] is False
        assert subsystem["copilot CLI"]["passed"] is False
        assert subsystem["copilot auth"]["passed"] is False
        assert subsystem["container images"]["passed"] is False

    def test_whitespace_auth_values_do_not_pass(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod
        from codeprobe.core import sandbox as codeprobe_sandbox

        _use_tool_paths(monkeypatch, tmp_path, ("claude", "copilot", "gh"))
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: True)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        for key in (
            "ANTHROPIC_API_KEY",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "COPILOT_API_KEY",
            "COPILOT_GITHUB_TOKEN",
            "GH_TOKEN",
            "OPENAI_API_KEY",
            "GITHUB_TOKEN",
        ):
            monkeypatch.setenv(key, " \t\n")

        def _fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
            return _FakeProc(1 if cmd[:2] == ["gh", "auth"] else 0)

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)

        claude = {r.name: r for r in run_checks(agent="claude")}
        copilot = {r.name: r for r in run_checks(agent="copilot")}
        codex = {r.name: r for r in run_checks(agent="codex")}

        assert claude["claude auth"].passed is False
        assert copilot["copilot auth"].passed is False
        assert codex["codex auth"].passed is False
        assert claude["ANTHROPIC_API_KEY"].passed is False
        assert codex["OPENAI_API_KEY"].passed is False
        assert claude["GitHub auth"].passed is False

    def test_private_ca_failure_does_not_print_path(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        ca_path = tmp_path / "secret-ca.pem"
        _use_claude_agent_path(monkeypatch, tmp_path)
        monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])

        assert result.exit_code == 2
        assert "FAIL  private CA files (unreadable: SSL_CERT_FILE)" in result.output
        assert str(ca_path) not in result.output

    def test_private_ca_option_failure_does_not_print_path(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        ca_path = tmp_path / "secret-ca.pem"
        _use_claude_agent_path(monkeypatch, tmp_path)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "doctor",
                "--agent",
                "claude",
                "--private-ca",
                str(ca_path),
                "--no-json",
            ],
        )

        assert result.exit_code == 2
        assert "FAIL  private CA files (unreadable: --private-ca)" in result.output
        assert str(ca_path) not in result.output

    def test_private_ca_option_unreadable_file_fails(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        ca_path = tmp_path / "secret-ca.pem"
        ca_path.write_text("certificate", encoding="utf-8")
        ca_path.chmod(0)
        _use_claude_agent_path(monkeypatch, tmp_path)

        runner = CliRunner()
        try:
            result = runner.invoke(
                main,
                [
                    "doctor",
                    "--agent",
                    "claude",
                    "--private-ca",
                    str(ca_path),
                    "--no-json",
                ],
            )
        finally:
            ca_path.chmod(0o600)

        assert result.exit_code == 2
        assert "FAIL  private CA files (unreadable: --private-ca)" in result.output
        assert str(ca_path) not in result.output

    def test_private_ca_env_unreadable_file_fails(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        ca_path = tmp_path / "secret-ca.pem"
        ca_path.write_text("certificate", encoding="utf-8")
        ca_path.chmod(0)
        _use_claude_agent_path(monkeypatch, tmp_path)
        monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))

        runner = CliRunner()
        try:
            result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])
        finally:
            ca_path.chmod(0o600)

        assert result.exit_code == 2
        assert "FAIL  private CA files (unreadable: SSL_CERT_FILE)" in result.output
        assert str(ca_path) not in result.output

    def test_private_ca_env_unreadable_dir_fails(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        ca_dir = tmp_path / "secret-ca-dir"
        ca_dir.mkdir()
        ca_dir.chmod(0)
        _use_claude_agent_path(monkeypatch, tmp_path)
        monkeypatch.setenv("SSL_CERT_DIR", str(ca_dir))

        runner = CliRunner()
        try:
            result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])
        finally:
            ca_dir.chmod(0o700)

        assert result.exit_code == 2
        assert "FAIL  private CA files (unreadable: SSL_CERT_DIR)" in result.output
        assert str(ca_dir) not in result.output

    def test_repo_option_validates_target_repo(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import subprocess

        from codeprobe.core import sandbox as codeprobe_sandbox

        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        _use_tool_paths(monkeypatch, tmp_path, ("claude",))
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setenv("HOME", str(tmp_path))

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "doctor",
                "--agent",
                "claude",
                "--repo",
                str(repo),
                "--no-json",
            ],
        )

        assert result.exit_code == 0, result.output
        assert "PASS  git repo" in result.output

    def test_proxy_values_are_not_printed(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        proxy = "https://user:secret-password@example.test:8443"
        _use_claude_agent_path(monkeypatch, tmp_path)
        monkeypatch.setenv("HTTPS_PROXY", proxy)

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])

        assert result.exit_code == 0, result.output
        assert "PASS  proxy variables (1 configured)" in result.output
        assert "secret-password" not in result.output
        assert proxy not in result.output

    def test_invalid_proxy_value_fails_without_leaking_value(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        proxy = "https://proxy.example\tsecret-password"
        _use_claude_agent_path(monkeypatch, tmp_path)
        monkeypatch.setenv("HTTPS_PROXY", proxy)

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])

        assert result.exit_code == 2
        assert "FAIL  proxy variables (invalid: HTTPS_PROXY)" in result.output
        assert proxy not in result.output

    def test_invalid_proxy_authority_forms_fail_without_leaking_value(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        _use_claude_agent_path(monkeypatch, tmp_path)
        runner = CliRunner()

        for proxy in (
            "http://[::1",
            "https://bad host.example:8443",
            "https://proxy.example:0",
            "https://proxy.example:8443/path",
            "https://proxy.example:8443?token=secret",
            "https://proxy.example:8443#secret",
        ):
            monkeypatch.setenv("HTTPS_PROXY", proxy)
            result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])
            assert result.exit_code == 2, result.output
            assert "FAIL  proxy variables (invalid: HTTPS_PROXY)" in result.output
            assert proxy not in result.output
            monkeypatch.delenv("HTTPS_PROXY", raising=False)

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("HTTPS_PROXY", "https://proxy.example:8443\n"),
            ("HTTPS_PROXY", " https://proxy.example:8443"),
            ("HTTPS_PROXY", "http://user name:secret@proxy.example:8080"),
            ("HTTPS_PROXY", "http://user:secret value@proxy.example:8080"),
            ("HTTPS_PROXY", "http://user\u00a0name:secret@proxy.example:8080"),
            ("NO_PROXY", "example.test,\ninternal.test"),
            ("NO_PROXY", "example.test, internal.test"),
            ("NO_PROXY", "example.test,"),
        ],
    )
    def test_proxy_controls_and_surrounding_whitespace_fail(
        self,
        monkeypatch: object,
        tmp_path: Path,
        key: str,
        value: str,
    ) -> None:
        _use_claude_agent_path(monkeypatch, tmp_path)
        monkeypatch.setenv(key, value)

        result = CliRunner().invoke(
            main, ["doctor", "--agent", "claude", "--no-json"]
        )

        assert result.exit_code == 2, result.output
        assert f"FAIL  proxy variables (invalid: {key})" in result.output
        assert value not in result.output

    def test_no_container_engine_blocks_selected_agent(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        from codeprobe.core import sandbox as codeprobe_sandbox
        from codeprobe.sandbox import runner as container_runner

        _use_claude_agent_path(monkeypatch, tmp_path)
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: False)
        monkeypatch.setattr(container_runner, "detect_engine", lambda: None)

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])

        assert result.exit_code == 2, result.output
        assert "FAIL  container images (no container engine configured)" in result.output

    def test_container_images_fail_when_configured_engine_lacks_images(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod
        from codeprobe.core import sandbox as codeprobe_sandbox
        from codeprobe.sandbox import runner as container_runner

        _use_tool_paths(monkeypatch, tmp_path, ("claude", "docker"))
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: False)
        monkeypatch.setattr(
            container_runner, "detect_engine", lambda: "/usr/bin/docker"
        )
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
        monkeypatch.setattr(container_runner, "image_available", lambda *a: False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("HOME", str(tmp_path))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])

        assert result.exit_code == 2, result.output
        assert "FAIL  container images (2 required image(s) missing)" in result.output
        assert "Run 'codeprobe bootstrap'." in result.output
        assert "Dockerfile" not in result.output

    def test_container_images_report_missing_image_configuration_details(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod
        from codeprobe.core import sandbox as codeprobe_sandbox
        from codeprobe.sandbox import runner as container_runner

        _use_tool_paths(monkeypatch, tmp_path, ("claude", "docker"))
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: False)
        monkeypatch.setattr(
            container_runner, "detect_engine", lambda: "/usr/bin/docker"
        )

        def unprepared_image_reference() -> str:
            raise ValueError(
                "Missing required image setting(s): CODEPROBE_IMAGE_NAMESPACE"
            )

        monkeypatch.setattr(
            container_runner,
            "agent_image_reference",
            unprepared_image_reference,
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("HOME", str(tmp_path))

        result = CliRunner().invoke(
            main, ["doctor", "--agent", "claude", "--no-json"]
        )

        assert result.exit_code == 2, result.output
        assert (
            "FAIL  container images (image configuration invalid: "
            "Missing required image setting(s): CODEPROBE_IMAGE_NAMESPACE)"
            in result.output
        )
        assert "CODEPROBE_AGENT_IMAGE" in result.output
        assert "CODEPROBE_SCORING_IMAGE" in result.output
        assert "Run 'codeprobe bootstrap'." in result.output
        assert "Dockerfile" not in result.output

    def test_container_images_report_malformed_image_configuration_details(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod
        from codeprobe.core import sandbox as codeprobe_sandbox
        from codeprobe.sandbox import runner as container_runner

        _use_tool_paths(monkeypatch, tmp_path, ("claude", "docker"))
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: False)
        monkeypatch.setattr(
            container_runner, "detect_engine", lambda: "/usr/bin/docker"
        )

        def malformed_image_reference() -> str:
            raise ValueError("CODEPROBE_IMAGE_REGISTRY has an invalid registry host")

        monkeypatch.setattr(
            container_runner,
            "agent_image_reference",
            malformed_image_reference,
        )
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("HOME", str(tmp_path))

        result = CliRunner().invoke(
            main, ["doctor", "--agent", "claude", "--no-json"]
        )

        assert result.exit_code == 2, result.output
        assert (
            "FAIL  container images (image configuration invalid: "
            "CODEPROBE_IMAGE_REGISTRY has an invalid registry host)"
            in result.output
        )
        assert "Run 'codeprobe bootstrap'." in result.output
        assert "Dockerfile" not in result.output

    def test_offline_ttl_failure_is_blocking_when_requested(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import importlib

        from codeprobe.cli.errors import DiagnosticError

        infra = importlib.import_module("codeprobe.cli.check_infra")

        def _fail_offline_preflight(*args: object, **kwargs: object) -> None:
            raise DiagnosticError(
                code="OFFLINE_PREFLIGHT_FAILED",
                message="bedrock: credential EXPIRED",
                diagnose_cmd="codeprobe check-infra offline --json",
                terminal=True,
            )

        _use_claude_agent_path(monkeypatch, tmp_path)
        monkeypatch.setattr(infra, "run_offline_preflight", _fail_offline_preflight)

        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "doctor",
                "--agent",
                "claude",
                "--offline",
                "--offline-expected-run-duration",
                "2h",
                "--no-json",
            ],
        )

        assert result.exit_code == 2
        assert "FAIL  offline credential TTL" in result.output
        assert "OFFLINE_PREFLIGHT_FAILED" in result.output

    def test_doctor_offline_scopes_ttl_to_selected_agent_backend(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import importlib

        infra = importlib.import_module("codeprobe.cli.check_infra")
        calls: list[tuple[str, ...]] = []

        def _capture_offline_preflight(
            expected_run_duration: str,
            backend_filter: tuple[str, ...] = (),
            *,
            echo: bool = True,
        ) -> None:
            calls.append(backend_filter)

        _use_claude_agent_path(monkeypatch, tmp_path)
        monkeypatch.setattr(
            infra,
            "run_offline_preflight",
            _capture_offline_preflight,
        )

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["doctor", "--agent", "claude", "--offline", "--no-json"],
        )

        assert result.exit_code == 0, result.output
        assert calls == [("anthropic",)]

    def test_copilot_offline_requires_byok_offline_env(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        from codeprobe.core import sandbox as codeprobe_sandbox

        _use_tool_paths(monkeypatch, tmp_path, ("copilot",))
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: True)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "github_pat_test")
        monkeypatch.setenv("HOME", str(tmp_path))
        for key in ("COPILOT_OFFLINE", "COPILOT_PROVIDER_BASE_URL", "COPILOT_MODEL"):
            monkeypatch.delenv(key, raising=False)

        runner = CliRunner()
        result = runner.invoke(
            main,
            ["doctor", "--agent", "copilot", "--offline", "--no-json"],
        )

        assert result.exit_code == 2, result.output
        assert "FAIL  offline credential TTL" in result.output
        assert "COPILOT_OFFLINE=true" in result.output

    def test_doctor_all_pass(self, monkeypatch: object, tmp_path: Path) -> None:
        _use_tool_paths(monkeypatch, tmp_path, ("claude", "copilot", "gh"))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        # Redirect HOME so the user-home-skills check doesn't trip on the
        # developer's local ``~/.claude/skills/`` tree (codeprobe-coa).
        monkeypatch.setenv("HOME", str(tmp_path))

        runner = CliRunner()
        # --no-json forces the legacy pretty "PASS" surface; CliRunner is
        # non-TTY so the default is now the single-envelope JSON mode.
        result = runner.invoke(main, ["doctor", "--no-json"])
        assert result.exit_code == 0
        assert "FAIL" not in result.output
        assert "PASS" in result.output

    def test_doctor_some_fail(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--no-json"])
        # Migrated to DiagnosticError(code=DOCTOR_CHECKS_FAILED) whose
        # catalog-declared exit code is 2.
        assert result.exit_code == 2
        assert "FAIL" in result.output

    def test_doctor_does_not_print_key_values(self, monkeypatch: object) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value-12345")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])
        assert "sk-secret-value-12345" not in result.output
