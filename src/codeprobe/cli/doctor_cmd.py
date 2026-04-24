"""Doctor command — checks environment readiness for codeprobe."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass

import click

from codeprobe.cli._output_helpers import (
    add_json_flags,
    emit_envelope,
    resolve_mode,
)


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    fix: str


def _check_tool(name: str, fix: str) -> CheckResult:
    found = shutil.which(name) is not None
    return CheckResult(
        name=f"{name} CLI",
        passed=found,
        detail="found" if found else "not found",
        fix=fix,
    )


def _check_env_key(key: str, fix: str) -> CheckResult:
    present = key in os.environ and len(os.environ[key]) > 0
    return CheckResult(
        name=key,
        passed=present,
        detail="set" if present else "not set",
        fix=fix,
    )


def _check_git_repo() -> CheckResult:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        is_repo = result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        is_repo = False
    return CheckResult(
        name="git repo",
        passed=is_repo,
        detail="inside git repo" if is_repo else "not a git repository",
        fix="Run 'git init' or cd into an existing git repository.",
    )


def _check_python_version() -> CheckResult:
    major, minor = sys.version_info[:2]
    ok = (major, minor) >= (3, 11)
    return CheckResult(
        name="Python version",
        passed=ok,
        detail=f"{major}.{minor}",
        fix="Install Python 3.11 or later. See https://www.python.org/downloads/",
    )


def run_checks() -> list[CheckResult]:
    """Run all environment checks and return results."""
    return [
        _check_tool(
            "claude",
            "Install Claude Code: https://docs.anthropic.com/en/docs/claude-code",
        ),
        _check_tool(
            "copilot",
            "Install GitHub Copilot CLI: https://github.com/github/gh-copilot",
        ),
        _check_tool(
            "codex", "Install OpenAI Codex CLI: https://github.com/openai/codex"
        ),
        _check_env_key(
            "ANTHROPIC_API_KEY", "Set ANTHROPIC_API_KEY in your environment."
        ),
        _check_env_key("OPENAI_API_KEY", "Set OPENAI_API_KEY in your environment."),
        _check_env_key(
            "GITHUB_TOKEN",
            "Set GITHUB_TOKEN in your environment. See https://github.com/settings/tokens",
        ),
        _check_git_repo(),
        _check_python_version(),
    ]


@click.command("doctor")
@add_json_flags
def doctor(
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Check environment readiness for running codeprobe."""
    mode = resolve_mode(
        "doctor", json_flag, no_json_flag, json_lines_flag,
    )

    results = run_checks()
    any_failed = any(not r.passed for r in results)

    if mode.mode == "pretty":
        for r in results:
            if r.passed:
                click.echo(f"  PASS  {r.name} ({r.detail})")
            else:
                click.echo(f"  FAIL  {r.name} ({r.detail})")
                click.echo(f"        -> {r.fix}")
        if any_failed:
            raise SystemExit(1)
        return

    # Envelope / NDJSON mode — skip pretty, emit a single envelope.
    exit_code = 1 if any_failed else 0
    emit_envelope(
        command="doctor",
        ok=not any_failed,
        exit_code=exit_code,
        data={
            "checks": [asdict(r) for r in results],
            "any_failed": any_failed,
        },
    )
    if any_failed:
        raise SystemExit(exit_code)
