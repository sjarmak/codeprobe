"""Tests for the doctor command."""

from __future__ import annotations

from click.testing import CliRunner

from codeprobe.cli import main
from codeprobe.cli.doctor_cmd import run_checks


class TestDoctorChecks:
    """Unit tests for individual check functions."""

    def test_tool_found(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
        results = run_checks()
        tool_results = [r for r in results if r.name.endswith("CLI")]
        assert all(r.passed for r in tool_results)

    def test_tool_not_found(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        results = run_checks()
        tool_results = [r for r in results if r.name.endswith("CLI")]
        assert all(not r.passed for r in tool_results)
        assert all(r.fix for r in tool_results)

    def test_env_key_present(self, monkeypatch: object) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        results = run_checks()
        key_results = [
            r
            for r in results
            if r.name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN")
        ]
        assert all(r.passed for r in key_results)

    def test_env_key_absent(self, monkeypatch: object) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        results = run_checks()
        key_results = [
            r
            for r in results
            if r.name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN")
        ]
        assert all(not r.passed for r in key_results)

    def test_env_key_empty_string(self, monkeypatch: object) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        results = run_checks()
        r = next(r for r in results if r.name == "ANTHROPIC_API_KEY")
        assert not r.passed

    def test_python_version_passes(self) -> None:
        """Current test environment should be >= 3.11."""
        results = run_checks()
        r = next(r for r in results if r.name == "Python version")
        assert r.passed

    def test_python_version_too_old(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(mod.sys, "version_info", (3, 10, 0, "final", 0))
        results = run_checks()
        r = next(r for r in results if r.name == "Python version")
        assert not r.passed

    def test_git_repo_check(self) -> None:
        """Running in codeprobe repo, should pass."""
        results = run_checks()
        r = next(r for r in results if r.name == "git repo")
        assert r.passed

    def test_git_not_found(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        def _raise(*args, **kwargs):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(mod.subprocess, "run", _raise)
        results = run_checks()
        r = next(r for r in results if r.name == "git repo")
        assert not r.passed


class TestDoctorCLI:
    """Integration tests for the CLI command."""

    def test_doctor_all_pass(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/" + name)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")

        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 0
        assert "FAIL" not in result.output
        assert "PASS" in result.output

    def test_doctor_some_fail(self, monkeypatch: object) -> None:
        import codeprobe.cli.doctor_cmd as mod

        monkeypatch.setattr(mod.shutil, "which", lambda name: None)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])
        assert result.exit_code == 1
        assert "FAIL" in result.output

    def test_doctor_does_not_print_key_values(self, monkeypatch: object) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-value-12345")
        runner = CliRunner()
        result = runner.invoke(main, ["doctor"])
        assert "sk-secret-value-12345" not in result.output
