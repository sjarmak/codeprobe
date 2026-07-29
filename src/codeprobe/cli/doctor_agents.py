"""Agent selection and authentication checks for ``codeprobe doctor``."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

from codeprobe.adapters.protocol import quarantine_message
from codeprobe.cli import doctor_env


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    fix: str
    # Advisory results do NOT count toward `any_failed`; passing advisory
    # agent checks render as neutral INFO instead of selected-path PASS.
    warn_only: bool = False


_DOCTOR_AGENT_ORDER: tuple[str, ...] = ("claude", "copilot", "codex")
_AUTO_AGENT_ORDER: tuple[str, ...] = ("claude", "copilot")
_CLI_PROBE_ARGS: dict[str, tuple[str, ...]] = {
    "claude": ("--version",),
    "copilot": ("version",),
}
_COPILOT_AUTH_ENV_KEYS: tuple[str, ...] = (
    "COPILOT_GITHUB_TOKEN",
    "GH_TOKEN",
    "GITHUB_TOKEN",
)
_COPILOT_TOKEN_PREFIXES: tuple[str, ...] = ("gho_", "github_pat_", "ghu_")
_env_has_value = doctor_env.env_has_value


def _optional_result(result: CheckResult) -> CheckResult:
    return CheckResult(
        name=result.name,
        passed=result.passed,
        detail=result.detail,
        fix="",
        warn_only=True,
    )


def _check_tool(name: str, fix: str, *, required: bool = True) -> CheckResult:
    resolved = shutil.which(name)
    found = resolved is not None
    usable = False
    detail = "not found"
    if resolved is not None:
        try:
            path = Path(resolved)
            executable = path.is_file() and os.access(path, os.X_OK)
            if executable:
                probe_args = _CLI_PROBE_ARGS.get(name, ("--version",))
                probe = subprocess.run(
                    [resolved, *probe_args],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                usable = probe.returncode == 0
                detail = "found" if usable else "probe failed"
            else:
                detail = "not executable"
        except (OSError, subprocess.SubprocessError, ValueError):
            usable = False
            detail = "probe failed"
    result = CheckResult(
        name=f"{name} CLI",
        passed=usable,
        detail=detail if found else "not found",
        fix=fix,
    )
    return result if required else _optional_result(result)


def _github_auth_status() -> tuple[bool, str]:
    if _env_has_value("GITHUB_TOKEN"):
        return True, "GITHUB_TOKEN set"
    if shutil.which("gh") is not None:
        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return False, "no GitHub auth"
        if result.returncode == 0:
            return True, "gh auth ok (no GITHUB_TOKEN)"
    return False, "no GitHub auth"


def _copilot_token_supported(value: str) -> bool:
    return (
        bool(value)
        and not any(char.isspace() for char in value)
        and value.startswith(_COPILOT_TOKEN_PREFIXES)
    )


def _copilot_gh_auth_status() -> tuple[bool, str]:
    if shutil.which("gh") is None:
        return False, "no Copilot gh auth"
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False, "no Copilot gh auth"
    if result.returncode != 0:
        return False, "no Copilot gh auth"
    if not result.stdout.endswith("\n"):
        return False, "unsupported gh auth token"
    token = result.stdout[:-1].removesuffix("\r")
    if _copilot_token_supported(token):
        return True, "gh auth token ok"
    return False, "unsupported gh auth token"


def _check_github_access() -> CheckResult:
    """Return an advisory GitHub-auth result."""
    fix = "Set GITHUB_TOKEN or run gh auth login. Only needed for mining GitHub PRs."
    gh_ok, detail = _github_auth_status()
    return CheckResult(
        name="GitHub auth",
        passed=gh_ok,
        detail=detail,
        fix=fix,
        warn_only=True,
    )


def _check_claude_auth(*, required: bool) -> CheckResult:
    fix = (
        "Set ANTHROPIC_API_KEY, set CLAUDE_CODE_OAUTH_TOKEN, "
        "or run `claude login`."
    )
    if _env_has_value("ANTHROPIC_API_KEY") or _env_has_value(
        "CLAUDE_CODE_OAUTH_TOKEN"
    ):
        result = CheckResult(
            name="claude auth",
            passed=True,
            detail="environment auth configured",
            fix=fix,
        )
        return result if required else _optional_result(result)

    from codeprobe.adapters.claude import (
        _credentials_file_status,
        _effective_claude_config_dir,
    )

    status = _credentials_file_status(_effective_claude_config_dir())
    if status == "valid":
        result = CheckResult(
            name="claude auth",
            passed=True,
            detail="file credentials present",
            fix=fix,
        )
    else:
        detail = (
            "file credentials expired"
            if status == "expired"
            else "no environment or file credentials"
        )
        result = CheckResult(
            name="claude auth",
            passed=False,
            detail=detail,
            fix=fix,
        )
    return result if required else _optional_result(result)


def _copilot_env_auth_status() -> tuple[bool, str] | None:
    for key in _COPILOT_AUTH_ENV_KEYS:
        raw_value = os.environ.get(key)
        if raw_value is None:
            continue
        value = raw_value.strip()
        if value != raw_value or not _copilot_token_supported(value):
            return False, f"unsupported token in {key}"
        return True, f"{key} set"
    return None


def _check_copilot_auth(*, required: bool) -> CheckResult:
    fix = (
        "Set COPILOT_GITHUB_TOKEN, GH_TOKEN, or GITHUB_TOKEN with a "
        "supported Copilot CLI token, or run `gh auth login`."
    )
    env_status = _copilot_env_auth_status()
    if env_status is not None:
        passed, detail = env_status
        result = CheckResult(
            name="copilot auth",
            passed=passed,
            detail=detail,
            fix=fix,
        )
    else:
        gh_ok, detail = _copilot_gh_auth_status()
        result = CheckResult(
            name="copilot auth",
            passed=gh_ok,
            detail=detail,
            fix=fix,
        )
    return result if required else _optional_result(result)


def _check_openai_sdk(*, required: bool) -> CheckResult:
    found = find_spec("openai") is not None
    result = CheckResult(
        name="openai SDK",
        passed=found,
        detail="installed" if found else "not installed",
        fix="Install the OpenAI SDK with `pip install codeprobe[codex]`.",
    )
    return result if required else _optional_result(result)


def _check_codex_auth(*, required: bool) -> CheckResult:
    configured = _env_has_value("OPENAI_API_KEY")
    result = CheckResult(
        name="codex auth",
        passed=configured,
        detail="OPENAI_API_KEY set" if configured else "OPENAI_API_KEY not set",
        fix="Set OPENAI_API_KEY.",
    )
    return result if required else _optional_result(result)


def _check_codex_support(*, required: bool) -> CheckResult:
    result = CheckResult(
        name="codex adapter",
        passed=False,
        detail="quarantined",
        fix=quarantine_message("codex"),
    )
    return result if required else _optional_result(result)


def _agent_path_results(agent: str, *, required: bool) -> tuple[CheckResult, ...]:
    if agent == "claude":
        return (
            _check_tool(
                "claude",
                "Install Claude Code: https://docs.anthropic.com/en/docs/claude-code",
                required=required,
            ),
            _check_claude_auth(required=required),
        )
    if agent == "copilot":
        return (
            _check_tool(
                "copilot",
                "Install GitHub Copilot CLI: "
                "https://docs.github.com/copilot/how-tos/set-up/install-copilot-cli",
                required=required,
            ),
            _check_copilot_auth(required=required),
        )
    if agent == "codex":
        return (
            _check_codex_support(required=required),
            _check_openai_sdk(required=required),
            _check_codex_auth(required=required),
        )
    raise ValueError(f"Unknown doctor agent {agent!r}")


def _agent_usable(agent: str) -> bool:
    return all(result.passed for result in _agent_path_results(agent, required=False))


def _select_agent(agent: str | None) -> str | None:
    if agent is not None:
        selected = agent.strip().lower()
        if selected not in _DOCTOR_AGENT_ORDER:
            raise ValueError(f"Unknown doctor agent {agent!r}")
        return selected
    for candidate in _AUTO_AGENT_ORDER:
        if _agent_usable(candidate):
            return candidate
    return None


def _check_selected_agent(
    selected_agent: str | None, *, explicit: bool
) -> CheckResult:
    if selected_agent is None:
        return CheckResult(
            name="selected agent",
            passed=False,
            detail="no supported agent path usable",
            fix=(
                "Install and authenticate Claude Code or GitHub Copilot CLI, "
                "or run `codeprobe doctor --agent <agent>` to diagnose one path."
            ),
        )
    detail = (
        f"{selected_agent} selected"
        if explicit
        else f"{selected_agent} auto-selected"
    )
    return CheckResult(
        name="selected agent",
        passed=True,
        detail=detail,
        fix="",
    )
