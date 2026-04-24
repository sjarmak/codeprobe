"""Doctor command — checks environment readiness for codeprobe."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

import click

from codeprobe import __version__
from codeprobe.config.defaults import compact_budget_bytes


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


def _llm_available(results: list[CheckResult]) -> bool:
    """Return True when at least one model CLI + its API key are present."""
    by_name = {r.name: r for r in results}
    claude_ready = (
        by_name.get("claude CLI", CheckResult("", False, "", "")).passed
        and by_name.get("ANTHROPIC_API_KEY", CheckResult("", False, "", "")).passed
    )
    codex_ready = (
        by_name.get("codex CLI", CheckResult("", False, "", "")).passed
        and by_name.get("OPENAI_API_KEY", CheckResult("", False, "", "")).passed
    )
    return claude_ready or codex_ready


def _build_compact_envelope(results: list[CheckResult]) -> dict[str, object]:
    """Build a ≤2 KB JSON envelope for SKILL.md preflight substitution."""
    by_name = {r.name: r for r in results}
    gh_auth_ok = by_name.get(
        "GITHUB_TOKEN", CheckResult("", False, "", "")
    ).passed
    sourcegraph_token_present = any(
        os.environ.get(k) for k in (
            "SOURCEGRAPH_TOKEN", "SRC_ACCESS_TOKEN", "SOURCEGRAPH_ACCESS_TOKEN",
        )
    )
    any_failed = any(not r.passed for r in results)

    envelope: dict[str, object] = {
        "record_type": "doctor",
        "ok": not any_failed,
        "command": "doctor",
        "version": __version__,
        "schema_version": 1,
        "exit_code": 1 if any_failed else 0,
        "warnings": [],
        "next_steps": [],
        "error": None,
        "data": {
            "tenant": None,
            "tenant_source": "default",
            "llm_available": _llm_available(results),
            "gh_auth_ok": gh_auth_ok,
            "sourcegraph_token_present": sourcegraph_token_present,
        },
    }
    return envelope


def _build_full_envelope(results: list[CheckResult]) -> dict[str, object]:
    """Full envelope for ``--json`` without ``--compact``."""
    any_failed = any(not r.passed for r in results)
    subsystem_status = [
        {
            "name": r.name,
            "passed": r.passed,
            "detail": r.detail,
            "fix": r.fix if not r.passed else "",
        }
        for r in results
    ]
    envelope = _build_compact_envelope(results)
    envelope["data"] = {  # type: ignore[index]
        **envelope["data"],  # type: ignore[dict-item]
        "subsystem_status": subsystem_status,
    }
    envelope["ok"] = not any_failed
    return envelope


@click.command("doctor")
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Emit a structured JSON envelope instead of plain text.",
)
@click.option(
    "--compact",
    is_flag=True,
    default=False,
    help=(
        "With --json, emit a minimal envelope (<=2048 bytes) suitable for "
        "SKILL.md substitution. No effect without --json."
    ),
)
def doctor(json_output: bool, compact: bool) -> None:
    """Check environment readiness for running codeprobe."""
    results = run_checks()
    any_failed = any(not r.passed for r in results)

    if json_output:
        envelope = (
            _build_compact_envelope(results)
            if compact
            else _build_full_envelope(results)
        )
        payload = json.dumps(envelope, sort_keys=True, separators=(",", ":"))

        if compact:
            # Hard budget: trim subsystem_status style extras before emit.
            budget = compact_budget_bytes()
            if len(payload.encode("utf-8")) > budget:
                # Degrade gracefully — drop optional fields until we fit.
                minimal = {
                    "record_type": "doctor",
                    "ok": not any_failed,
                    "command": "doctor",
                    "version": envelope["version"],
                    "schema_version": 1,
                    "exit_code": 1 if any_failed else 0,
                    "error": None,
                    "data": envelope["data"],
                }
                payload = json.dumps(
                    minimal, sort_keys=True, separators=(",", ":")
                )

        click.echo(payload)
        if any_failed:
            raise SystemExit(1)
        return

    for r in results:
        if r.passed:
            click.echo(f"  PASS  {r.name} ({r.detail})")
        else:
            click.echo(f"  FAIL  {r.name} ({r.detail})")
            click.echo(f"        -> {r.fix}")

    if any_failed:
        raise SystemExit(1)
