"""Tests for the doctor command."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.cli.doctor_cmd import (
    _any_failed,
    _build_compact_envelope,
    run_checks,
)


class _FakeProc:
    """Minimal subprocess.CompletedProcess stand-in for mocked runs."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


class TestDoctorChecks:
    """Unit tests for individual check functions."""

    def test_tool_found(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
        # Short-circuit the GitHub check so no real `gh auth status` runs.
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        results = run_checks()
        tool_results = [r for r in results if r.name.endswith("CLI")]
        assert all(r.passed for r in tool_results)

    def test_tool_not_found(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        results = run_checks()
        tool_results = [r for r in results if r.name.endswith("CLI")]
        assert all(not r.passed for r in tool_results)
        assert all(r.warn_only for r in tool_results)
        selected = next(r for r in results if r.name == "selected agent")
        assert selected.passed is False
        assert selected.fix

    def test_env_key_present(self, monkeypatch: object) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        results = run_checks()
        key_results = [
            r
            for r in results
            if r.name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GitHub auth")
        ]
        assert len(key_results) == 3
        assert all(r.passed for r in key_results)

    def test_env_key_absent(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        results = run_checks()
        key_results = [
            r
            for r in results
            if r.name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GitHub auth")
        ]
        assert len(key_results) == 3
        assert all(not r.passed for r in key_results)

    def test_env_key_empty_string(self, monkeypatch: object) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        results = run_checks()
        r = next(r for r in results if r.name == "ANTHROPIC_API_KEY")
        assert not r.passed

    def test_python_version_passes(self, monkeypatch: object) -> None:
        """Current test environment should be >= 3.11."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        results = run_checks()
        r = next(r for r in results if r.name == "Python version")
        assert r.passed

    def test_python_version_too_old(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setattr(mod.sys, "version_info", (3, 10, 0, "final", 0))
        results = run_checks()
        r = next(r for r in results if r.name == "Python version")
        assert not r.passed

    def test_git_repo_check(self, monkeypatch: object) -> None:
        """Running in codeprobe repo, should pass."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        results = run_checks()
        r = next(r for r in results if r.name == "git repo")
        assert r.passed

    def test_git_not_found(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        def _raise(*args, **kwargs):
            raise FileNotFoundError("git not found")

        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setattr(mod.subprocess, "run", _raise)
        results = run_checks()
        r = next(r for r in results if r.name == "git repo")
        assert not r.passed


class TestDoctorCLI:
    """Integration tests for the CLI command."""

    def test_selected_agent_ignores_absent_unselected_agents(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])

        assert result.exit_code == 0, result.output
        assert "PASS  claude CLI" in result.output
        assert "INFO  copilot CLI" in result.output
        assert "FAIL  copilot CLI" not in result.output

    def test_doctor_auto_selects_one_usable_agent(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--no-json"])

        assert result.exit_code == 0, result.output
        assert "PASS  selected agent (claude auto-selected)" in result.output
        assert "FAIL  copilot CLI" not in result.output

    def test_selected_agent_requires_auth(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])

        assert result.exit_code == 2
        assert "FAIL  claude auth" in result.output
        assert str(tmp_path) not in result.output

    def test_private_ca_failure_does_not_print_path(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        ca_path = tmp_path / "secret-ca.pem"
        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("SSL_CERT_FILE", str(ca_path))
        monkeypatch.setenv("HOME", str(tmp_path))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])

        assert result.exit_code == 2
        assert "FAIL  private CA files (unreadable: SSL_CERT_FILE)" in result.output
        assert str(ca_path) not in result.output

    def test_proxy_values_are_not_printed(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        proxy = "https://user:secret-password@example.test:8443"
        for key in mod._PROXY_ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("HTTPS_PROXY", proxy)
        monkeypatch.setenv("HOME", str(tmp_path))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])

        assert result.exit_code == 0, result.output
        assert "PASS  proxy variables (1 configured)" in result.output
        assert "secret-password" not in result.output
        assert proxy not in result.output

    def test_container_images_fail_when_configured_engine_lacks_images(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod
        from codeprobe.core import sandbox as codeprobe_sandbox
        from codeprobe.sandbox import runner as container_runner

        def _which(name: str) -> str | None:
            if name in ("claude", "docker"):
                return "/usr/bin/" + name
            return None

        monkeypatch.setattr(mod.shutil, "which", _which)
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: False)
        monkeypatch.setattr(
            container_runner, "detect_engine", lambda: "/usr/bin/docker"
        )
        monkeypatch.setattr(container_runner, "image_available", lambda *a: False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("HOME", str(tmp_path))

        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--agent", "claude", "--no-json"])

        assert result.exit_code == 2, result.output
        assert "FAIL  container images (2 required image(s) missing)" in result.output

    def test_offline_ttl_failure_is_blocking_when_requested(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import importlib

        import codeprobe.cli.doctor_cmd as mod
        from codeprobe.cli.errors import DiagnosticError

        infra = importlib.import_module("codeprobe.cli.check_infra")

        def _fail_offline_preflight(*args: object, **kwargs: object) -> None:
            raise DiagnosticError(
                code="OFFLINE_PREFLIGHT_FAILED",
                message="bedrock: credential EXPIRED",
                diagnose_cmd="codeprobe check-infra offline --json",
                terminal=True,
            )

        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: "/usr/bin/claude" if name == "claude" else None,
        )
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
        monkeypatch.setattr(infra, "run_offline_preflight", _fail_offline_preflight)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("HOME", str(tmp_path))

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

    def test_doctor_all_pass(self, monkeypatch: object, tmp_path: Path) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
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


class TestApiKeyWarnDemotion:
    """A missing raw API key is advisory; selected-agent auth determines
    readiness for the active provider path."""

    def test_key_warn_only_when_cli_present(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        # GitHub auth is now always advisory (warn_only), so it can no
        # longer flip `_any_failed` either way — but `which` is patched to
        # report every tool (including `gh`) as present, so explicitly
        # control the auth-detection outcome instead of depending on
        # whatever GITHUB_TOKEN happens to be set in the ambient environment
        # (e.g. CI, where it is not exported) or shelling out to a real `gh`
        # binary. Mocking subprocess.run also keeps the git-repo check's
        # `git rev-parse` deterministic. Mirrors
        # TestDoctorCLI.test_doctor_all_pass.
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
        by_name = {r.name: r for r in run_checks()}
        assert by_name["ANTHROPIC_API_KEY"].passed is False
        assert by_name["ANTHROPIC_API_KEY"].warn_only is True
        assert by_name["OPENAI_API_KEY"].warn_only is True
        # The advisory key does not flip doctor to a failure.
        assert _any_failed(list(by_name.values())) is False

    def test_key_hard_fail_when_no_cli(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        by_name = {r.name: r for r in run_checks()}
        # No usable agent path exists, but the raw key check remains advisory.
        assert by_name["ANTHROPIC_API_KEY"].warn_only is True
        assert by_name["selected agent"].passed is False
        assert _any_failed(list(by_name.values())) is True

    def test_present_key_is_never_warn_only(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        by_name = {r.name: r for r in run_checks()}
        assert by_name["ANTHROPIC_API_KEY"].passed is True
        assert by_name["ANTHROPIC_API_KEY"].warn_only is False

    def test_doctor_pretty_shows_warn_and_exits_zero(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setenv("HOME", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--no-json"])
        assert result.exit_code == 0, result.output
        assert "WARN  ANTHROPIC_API_KEY" in result.output


class TestGithubAccessCheck:
    """GitHub auth is advisory: GITHUB_TOKEN or `gh auth status`, always
    warn_only — missing GitHub auth never flips doctor to exit 2 because
    local mining paths are first-class (codeprobe-f7rl.17)."""

    def test_no_auth_warns_but_never_fails(
        self, monkeypatch: object
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: None if name == "gh" else "/usr/bin/" + name,
        )
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        results = run_checks()
        gh = next(r for r in results if r.name == "GitHub auth")
        assert gh.passed is False
        assert gh.warn_only is True
        assert gh.detail == "no GitHub auth"
        assert "gh auth login" in gh.fix
        assert _any_failed(results) is False

    def test_gh_auth_passes_without_token(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
        calls: list[list[str]] = []

        def _fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
            calls.append(cmd)
            return _FakeProc(0)

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)
        results = run_checks()
        gh = next(r for r in results if r.name == "GitHub auth")
        assert gh.passed is True
        assert gh.detail == "gh auth ok (no GITHUB_TOKEN)"
        assert ["gh", "auth", "status"] in calls

    def test_token_set_passes_without_gh(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        results = run_checks()
        gh = next(r for r in results if r.name == "GitHub auth")
        assert gh.passed is True
        assert gh.detail == "GITHUB_TOKEN set"

    def test_gh_auth_status_nonzero_is_not_passed(
        self, monkeypatch: object
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)

        def _fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
            # gh present but not authenticated; keep git-repo passing.
            return _FakeProc(1 if cmd[:2] == ["gh", "auth"] else 0)

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)
        results = run_checks()
        gh = next(r for r in results if r.name == "GitHub auth")
        assert gh.passed is False
        assert gh.warn_only is True

    def test_compact_envelope_gh_auth_ok(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        # No token, no gh → False.
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: None if name == "gh" else "/usr/bin/" + name,
        )
        envelope = _build_compact_envelope(run_checks())
        assert envelope["data"]["gh_auth_ok"] is False

        # No token, gh authenticated → True.
        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
        envelope = _build_compact_envelope(run_checks())
        assert envelope["data"]["gh_auth_ok"] is True

        # Token set, no gh → True.
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        envelope = _build_compact_envelope(run_checks())
        assert envelope["data"]["gh_auth_ok"] is True

    def test_doctor_exits_zero_in_clean_env(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        """CI-portability regression: no maintainer credentials at all, agent
        CLIs present → doctor exits 0 (codeprobe-f7rl.17)."""
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: None if name == "gh" else "/usr/bin/" + name,
        )
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--no-json"])
        assert result.exit_code == 0, result.output
        assert "WARN  GitHub auth" in result.output
        assert "FAIL" not in result.output
