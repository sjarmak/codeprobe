"""Agent selection and authentication checks for the doctor command."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.cli.doctor_cmd import (
    _any_failed,
    _build_compact_envelope,
    run_checks,
)
from tests._doctor_helpers import (
    _fake_cli,
    _FakeProc,
    _use_tool_paths,
)


class TestDoctorChecks:
    """Unit tests for individual check functions."""

    def test_tool_found(self, monkeypatch: object, tmp_path: Path) -> None:
        _use_tool_paths(
            monkeypatch,
            tmp_path,
            ("claude", "copilot", "gh"),
        )
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

    def test_env_key_whitespace_string(self, monkeypatch: object) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", " \t\n")
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

    def test_selected_cli_path_must_be_executable(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod
        from codeprobe.core import sandbox as codeprobe_sandbox

        fake_cli = tmp_path / "claude"
        fake_cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_cli.chmod(0o644)

        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: str(fake_cli) if name == "claude" else None,
        )
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setenv("HOME", str(tmp_path))

        results = run_checks(agent="claude")

        cli = next(r for r in results if r.name == "claude CLI")
        assert cli.passed is False
        assert cli.warn_only is False
        assert _any_failed(results) is True

    def test_selected_cli_path_must_pass_active_probe(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod
        from codeprobe.core import sandbox as codeprobe_sandbox

        false_cli = _fake_cli(tmp_path, "claude", returncode=1)
        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: str(false_cli) if name == "claude" else None,
        )
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: True)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("HOME", str(tmp_path))

        results = run_checks(agent="claude")
        by_name = {r.name: r for r in results}
        compact = _build_compact_envelope(results)

        assert by_name["claude CLI"].passed is False
        assert by_name["claude CLI"].warn_only is False
        assert _any_failed(results) is True
        assert compact["ok"] is False
        assert compact["data"]["llm_available"] is False

    def test_claude_auth_rejects_symlinked_container_session_credentials(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod
        from codeprobe.adapters.claude import ClaudeAdapter
        from codeprobe.core import containment
        from codeprobe.core import sandbox as codeprobe_sandbox
        from codeprobe.sandbox import runner as container_runner

        _use_tool_paths(monkeypatch, tmp_path, ("claude",))
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
        monkeypatch.setattr(container_runner, "image_available", lambda *a: True)
        monkeypatch.setattr(
            containment,
            "active_plan",
            lambda: containment.ContainmentPlan(
                mode="container", engine="/usr/bin/docker"
            ),
        )
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        fake_home = tmp_path / "home"
        real_claude = fake_home / ".claude"
        real_claude.mkdir(parents=True)
        host_cred = real_claude / ".credentials.json"
        host_cred.write_text("{}", encoding="utf-8")
        monkeypatch.setenv("HOME", str(fake_home))

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

        by_name = {result.name: result for result in run_checks(agent="claude")}

        assert by_name["claude auth"].passed is False
        assert "host file credentials present" in by_name["claude auth"].detail
        assert "containerized CLAUDE_CONFIG_DIR" in by_name["claude auth"].detail
        assert "symlink" in by_name["claude auth"].detail
        assert "claude login" in by_name["claude auth"].fix

    def test_copilot_auth_uses_supported_env_names(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        from codeprobe.core import sandbox as codeprobe_sandbox

        _use_tool_paths(monkeypatch, tmp_path, ("copilot",))
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: True)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("COPILOT_API_KEY", "obsolete-secret")
        for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)

        by_name = {r.name: r for r in run_checks(agent="copilot")}

        assert by_name["copilot auth"].passed is False
        assert "COPILOT_API_KEY" not in by_name["copilot auth"].detail
        assert "COPILOT_API_KEY" not in by_name["copilot auth"].fix

        monkeypatch.delenv("COPILOT_API_KEY", raising=False)
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "github_pat_test")

        by_name = {r.name: r for r in run_checks(agent="copilot")}

        assert by_name["copilot auth"].passed is True
        assert by_name["copilot auth"].detail == "COPILOT_GITHUB_TOKEN set"

    @pytest.mark.parametrize(
        "key,good_value",
        [
            ("COPILOT_GITHUB_TOKEN", "gho_test"),
            ("GH_TOKEN", "github_pat_test"),
            ("GITHUB_TOKEN", "ghu_test"),
        ],
    )
    def test_copilot_auth_validates_supported_token_prefixes(
        self,
        monkeypatch: object,
        tmp_path: Path,
        key: str,
        good_value: str,
    ) -> None:
        from codeprobe.core import sandbox as codeprobe_sandbox

        _use_tool_paths(monkeypatch, tmp_path, ("copilot",))
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: True)
        monkeypatch.setenv("HOME", str(tmp_path))
        for env_key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            monkeypatch.delenv(env_key, raising=False)
        monkeypatch.delenv("COPILOT_API_KEY", raising=False)

        monkeypatch.setenv(key, good_value)
        by_name = {r.name: r for r in run_checks(agent="copilot")}

        assert by_name["copilot auth"].passed is True
        assert by_name["copilot auth"].detail == f"{key} set"

        for bad_value in ("ghp_test", "plain-token", " github_pat_test "):
            monkeypatch.setenv(key, bad_value)
            by_name = {r.name: r for r in run_checks(agent="copilot")}
            assert by_name["copilot auth"].passed is False
            assert by_name["copilot auth"].detail == f"unsupported token in {key}"
            assert "copilot login" not in by_name["copilot auth"].fix

    @pytest.mark.parametrize(
        "token",
        ("gho_test-secret", "ghu_test-secret", "github_pat_test-secret"),
    )
    def test_copilot_auth_uses_supported_gh_token_without_leaking(
        self, monkeypatch: object, token: str
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: "/usr/bin/gh" if name == "gh" else None,
        )
        calls: list[list[str]] = []

        def _fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
            calls.append(cmd)
            return _FakeProc(0, stdout=f"{token}\n")

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)

        result = mod._check_copilot_auth(required=True)

        assert result.passed is True
        assert result.detail == "gh auth token ok"
        assert ["gh", "auth", "token"] in calls
        assert ["gh", "auth", "status"] not in calls
        assert token not in result.detail
        assert token not in result.fix

    @pytest.mark.parametrize("line_ending", ("\n", "\r\n"))
    def test_copilot_auth_accepts_exact_gh_token_line_ending(
        self, monkeypatch: object, line_ending: str
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        token = "github_pat_test-secret"
        for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: "/usr/bin/gh" if name == "gh" else None,
        )

        def _fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
            return _FakeProc(0, stdout=f"{token}{line_ending}")

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)

        result = mod._check_copilot_auth(required=True)

        assert result.passed is True
        assert result.detail == "gh auth token ok"
        assert token not in result.detail
        assert token not in result.fix

    @pytest.mark.parametrize(
        "stdout",
        (
            "github_pat_test-secret",
            " github_pat_test-secret\n",
            "github_pat_test-secret \n",
            "github_pat_test-secret\n\n",
            "github_pat_test-secret\r\nextra\n",
        ),
    )
    def test_copilot_auth_rejects_non_exact_gh_token_stdout_without_leaking(
        self, monkeypatch: object, stdout: str
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        token = "github_pat_test-secret"
        for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: "/usr/bin/gh" if name == "gh" else None,
        )

        def _fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
            return _FakeProc(0, stdout=stdout)

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)

        result = mod._check_copilot_auth(required=True)

        assert result.passed is False
        assert result.detail == "unsupported gh auth token"
        assert "copilot login" not in result.fix
        assert token not in result.detail
        assert token not in result.fix

    @pytest.mark.parametrize("token", ("ghp_test-secret", "plain-secret", " \t\n"))
    def test_copilot_auth_rejects_unsupported_gh_token_without_leaking(
        self, monkeypatch: object, token: str
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: "/usr/bin/gh" if name == "gh" else None,
        )

        def _fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
            return _FakeProc(0, stdout=f"{token}\n")

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)

        result = mod._check_copilot_auth(required=True)

        assert result.passed is False
        assert result.detail == "unsupported gh auth token"
        assert "copilot login" not in result.fix
        if token.strip():
            assert token.strip() not in result.detail
            assert token.strip() not in result.fix

    @pytest.mark.parametrize("failure", ("nonzero", "oserror", "timeout"))
    def test_copilot_auth_gh_token_failure_is_secret_free(
        self, monkeypatch: object, failure: str
    ) -> None:
        import subprocess

        import codeprobe.cli.doctor_cmd as mod

        secret = "github_pat_test-secret"
        for key in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setattr(
            mod.shutil,
            "which",
            lambda name: "/usr/bin/gh" if name == "gh" else None,
        )

        def _fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
            if failure == "oserror":
                raise OSError(f"exec failed for {secret}")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(cmd, 5, output=secret)
            return _FakeProc(1, stdout=f"{secret}\n")

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)

        result = mod._check_copilot_auth(required=True)

        assert result.passed is False
        assert result.detail == "no Copilot gh auth"
        assert secret not in result.detail
        assert secret not in result.fix


class TestApiKeyWarnDemotion:
    """A missing raw API key is advisory; selected-agent auth determines
    readiness for the active provider path."""

    def test_key_warn_only_when_cli_present(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        _use_tool_paths(monkeypatch, tmp_path, ("claude", "copilot", "gh"))
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
        monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "github_pat_test")
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

    def test_present_key_is_never_warn_only(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        _use_tool_paths(monkeypatch, tmp_path, ("claude", "copilot", "gh"))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        by_name = {r.name: r for r in run_checks()}
        assert by_name["ANTHROPIC_API_KEY"].passed is True
        assert by_name["ANTHROPIC_API_KEY"].warn_only is False

    def test_doctor_pretty_shows_warn_and_exits_zero(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        _use_tool_paths(monkeypatch, tmp_path, ("claude", "copilot", "gh"))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("GITHUB_TOKEN", "github_pat_test")
        monkeypatch.setenv("HOME", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--no-json"])
        assert result.exit_code == 0, result.output
        assert "WARN  ANTHROPIC_API_KEY" in result.output


class TestGithubAccessCheck:
    """GitHub auth is advisory: GITHUB_TOKEN or `gh auth status`, always
    warn_only — missing GitHub auth never flips doctor to exit 2 because
    local mining paths are first-class (codeprobe-f7rl.17)."""

    def test_no_auth_warns_while_selected_agent_still_fails(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        _use_tool_paths(monkeypatch, tmp_path, ("claude", "copilot"))
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        results = run_checks()
        gh = next(r for r in results if r.name == "GitHub auth")
        assert gh.passed is False
        assert gh.warn_only is True
        assert gh.detail == "no GitHub auth"
        assert "gh auth login" in gh.fix
        selected = next(r for r in results if r.name == "selected agent")
        assert selected.passed is False
        assert _any_failed(results) is True

    def test_gh_auth_passes_without_token(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        _use_tool_paths(monkeypatch, tmp_path, ("claude", "copilot", "gh"))
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
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        _use_tool_paths(monkeypatch, tmp_path, ("claude", "copilot", "gh"))

        def _fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
            # gh present but not authenticated; keep git-repo passing.
            return _FakeProc(1 if cmd[:2] == ["gh", "auth"] else 0)

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)
        results = run_checks()
        gh = next(r for r in results if r.name == "GitHub auth")
        assert gh.passed is False
        assert gh.warn_only is True

    def test_gh_auth_probe_oserror_is_safe_diagnostic(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        _use_tool_paths(monkeypatch, tmp_path, ("claude", "copilot", "gh"))

        def _fake_run(cmd: list[str], **kwargs: object) -> _FakeProc:
            if cmd[:2] == ["gh", "auth"]:
                raise OSError("exec format error")
            return _FakeProc(0)

        monkeypatch.setattr(mod.subprocess, "run", _fake_run)

        results = run_checks()
        gh = next(r for r in results if r.name == "GitHub auth")

        assert gh.passed is False
        assert gh.warn_only is True
        assert gh.detail == "no GitHub auth"

    def test_compact_envelope_gh_auth_ok(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        import codeprobe.cli.doctor_cmd as mod

        # No token, no gh → False.
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        _use_tool_paths(monkeypatch, tmp_path, ("claude", "copilot"))
        envelope = _build_compact_envelope(run_checks())
        assert envelope["data"]["gh_auth_ok"] is False

        # No token, gh authenticated → True.
        _use_tool_paths(monkeypatch, tmp_path, ("claude", "copilot", "gh"))
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _FakeProc(0))
        envelope = _build_compact_envelope(run_checks())
        assert envelope["data"]["gh_auth_ok"] is True

        # Token set, no gh → True.
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        envelope = _build_compact_envelope(run_checks())
        assert envelope["data"]["gh_auth_ok"] is True

    def test_doctor_fails_when_no_agent_path_is_authenticated(
        self, monkeypatch: object, tmp_path: Path
    ) -> None:
        """No GitHub auth stays advisory, but no usable agent path is blocking."""
        from codeprobe.core import sandbox as codeprobe_sandbox

        _use_tool_paths(monkeypatch, tmp_path, ("claude", "copilot"))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("COPILOT_API_KEY", raising=False)
        monkeypatch.setattr(codeprobe_sandbox, "is_sandboxed", lambda: True)
        monkeypatch.setenv("HOME", str(tmp_path))
        runner = CliRunner()
        result = runner.invoke(main, ["doctor", "--no-json"])
        assert result.exit_code == 2, result.output
        assert "FAIL  selected agent (no supported agent path usable)" in result.output
        assert "WARN  GitHub auth" in result.output
        assert "FAIL  GitHub auth" not in result.output
