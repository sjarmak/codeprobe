"""Doctor command — checks environment readiness for codeprobe."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from importlib.util import find_spec
from pathlib import Path

import click

from codeprobe import __version__
from codeprobe.adapters.protocol import quarantine_message
from codeprobe.cli._output_helpers import (
    add_json_flags,
    emit_envelope,
    resolve_mode,
)
from codeprobe.cli.errors import DiagnosticError
from codeprobe.config.defaults import compact_budget_bytes


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str
    fix: str
    # When True, a non-passing result is advisory and does NOT count toward
    # `any_failed` / the exit-2 DOCTOR_CHECKS_FAILED.
    warn_only: bool = False


_DOCTOR_AGENT_ORDER: tuple[str, ...] = ("claude", "copilot", "codex")
_AUTO_AGENT_ORDER: tuple[str, ...] = ("claude", "copilot")
_PROXY_ENV_KEYS: tuple[str, ...] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
    "all_proxy",
)
_PRIVATE_CA_FILE_ENV_KEYS: tuple[str, ...] = (
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
)
_PRIVATE_CA_DIR_ENV_KEYS: tuple[str, ...] = ("SSL_CERT_DIR",)


def _optional_result(result: CheckResult) -> CheckResult:
    if result.passed:
        return result
    return CheckResult(
        name=result.name,
        passed=False,
        detail=result.detail,
        fix="",
        warn_only=True,
    )


def _check_tool(name: str, fix: str, *, required: bool = True) -> CheckResult:
    found = shutil.which(name) is not None
    result = CheckResult(
        name=f"{name} CLI",
        passed=found,
        detail="found" if found else "not found",
        fix=fix,
    )
    return result if required else _optional_result(result)


def _check_env_key(key: str, fix: str, *, warn_only: bool = False) -> CheckResult:
    present = key in os.environ and len(os.environ[key]) > 0
    return CheckResult(
        name=key,
        passed=present,
        detail="set" if present else "not set",
        fix=fix,
        warn_only=warn_only and not present,
    )


def _github_auth_status() -> tuple[bool, str]:
    if len(os.environ.get("GITHUB_TOKEN", "")) > 0:
        return True, "GITHUB_TOKEN set"
    if shutil.which("gh") is not None:
        try:
            result = subprocess.run(
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False, "no GitHub auth"
        if result.returncode == 0:
            return True, "gh auth ok (no GITHUB_TOKEN)"
    return False, "no GitHub auth"


def _check_github_access() -> CheckResult:
    """Advisory GitHub-auth check: GITHUB_TOKEN or an authenticated gh CLI.

    Doctor is environment-readiness, not per-feature gating. GitHub is one
    mining source among several (local paths are first-class), so missing
    GitHub auth is always ``warn_only`` — rendered WARN, never exit 2.
    GitHub-requiring flows (e.g. mine's GitHub source path) fail loud at
    their own boundary.
    """
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
    result = _check_claude_auth_required()
    return result if required else _optional_result(result)


def _check_claude_auth_required() -> CheckResult:
    fix = (
        "Set ANTHROPIC_API_KEY, set CLAUDE_CODE_OAUTH_TOKEN, "
        "or run `claude login`."
    )
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get(
        "CLAUDE_CODE_OAUTH_TOKEN"
    ):
        return CheckResult(
            name="claude auth",
            passed=True,
            detail="environment auth configured",
            fix=fix,
        )

    from codeprobe.adapters.claude import (
        _credentials_file_status,
        _effective_claude_config_dir,
    )

    status = _credentials_file_status(_effective_claude_config_dir())
    if status == "valid":
        return CheckResult(
            name="claude auth",
            passed=True,
            detail="file credentials present",
            fix=fix,
        )
    detail = (
        "file credentials expired"
        if status == "expired"
        else "no environment or file credentials"
    )
    return CheckResult(
        name="claude auth",
        passed=False,
        detail=detail,
        fix=fix,
    )


def _check_copilot_auth(*, required: bool) -> CheckResult:
    result = _check_copilot_auth_required()
    return result if required else _optional_result(result)


def _check_copilot_auth_required() -> CheckResult:
    fix = "Set GITHUB_TOKEN, set COPILOT_API_KEY, or run `gh auth login`."
    if os.environ.get("COPILOT_API_KEY"):
        return CheckResult(
            name="copilot auth",
            passed=True,
            detail="COPILOT_API_KEY set",
            fix=fix,
        )
    gh_ok, detail = _github_auth_status()
    return CheckResult(
        name="copilot auth",
        passed=gh_ok,
        detail=detail,
        fix=fix,
    )


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
    result = CheckResult(
        name="codex auth",
        passed=bool(os.environ.get("OPENAI_API_KEY")),
        detail="OPENAI_API_KEY set"
        if os.environ.get("OPENAI_API_KEY")
        else "OPENAI_API_KEY not set",
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
                "Install GitHub Copilot CLI: https://github.com/github/gh-copilot",
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
    return all(r.passed for r in _agent_path_results(agent, required=False))


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


def _check_selected_agent(selected_agent: str | None, *, explicit: bool) -> CheckResult:
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


def _check_user_home_skills(*, required: bool = True) -> CheckResult:
    """Flag stale user-home codeprobe skills that need migration (codeprobe-coa)."""
    from codeprobe.cli.skills_cmd import stale_user_home_skills

    stale = stale_user_home_skills()
    if not stale:
        return CheckResult(
            name="user-home skills up to date",
            passed=True,
            detail="no stale user-home codeprobe skills detected",
            fix="",
        )
    names = ", ".join(r.old_name for r in stale)
    result = CheckResult(
        name="user-home skills up to date",
        passed=False,
        detail=(
            f"{len(stale)} stale user-home skill(s): {names}. "
            "Claude Code's skill resolver may pick the stale copy."
        ),
        fix="Run 'codeprobe skills migrate --dry-run' to preview, then "
        "'codeprobe skills migrate --yes' (TTY) or set "
        "CODEPROBE_SKILLS_MIGRATE=ack (CI) to apply.",
    )
    return result if required else _optional_result(result)


def _check_container_images() -> CheckResult:
    from codeprobe.core import sandbox as codeprobe_sandbox
    from codeprobe.sandbox import runner as container_runner

    if codeprobe_sandbox.is_sandboxed():
        return CheckResult(
            name="container images",
            passed=True,
            detail="already sandboxed",
            fix="",
        )

    engine = container_runner.detect_engine()
    if engine is None:
        return CheckResult(
            name="container images",
            passed=False,
            detail="no container engine configured",
            fix="",
            warn_only=True,
        )

    missing: list[str] = []
    for image in (
        container_runner.DEFAULT_AGENT_IMAGE,
        container_runner.DEFAULT_SCORING_IMAGE,
    ):
        if not container_runner.image_available(engine, image):
            missing.append(image)
    if not missing:
        return CheckResult(
            name="container images",
            passed=True,
            detail="agent and scoring images available",
            fix="",
        )
    return CheckResult(
        name="container images",
        passed=False,
        detail=f"{len(missing)} required image(s) missing",
        fix=(
            "Build missing images from the repository root: "
            "docker build -f src/codeprobe/sandbox/Dockerfile.agent "
            "-t codeprobe-agent:0.12 . && "
            "docker build -f src/codeprobe/sandbox/Dockerfile.scoring "
            "-t codeprobe-scoring:0.12 ."
        ),
    )


def _check_proxy_variables() -> CheckResult:
    configured = [key for key in _PROXY_ENV_KEYS if os.environ.get(key)]
    return CheckResult(
        name="proxy variables",
        passed=True,
        detail=(
            f"{len(configured)} configured"
            if configured
            else "not configured"
        ),
        fix="",
    )


def _check_private_ca_files() -> CheckResult:
    unreadable: list[str] = []
    for key in _PRIVATE_CA_FILE_ENV_KEYS:
        raw = os.environ.get(key)
        if raw and not Path(raw).is_file():
            unreadable.append(key)
    for key in _PRIVATE_CA_DIR_ENV_KEYS:
        raw = os.environ.get(key)
        if raw and not Path(raw).is_dir():
            unreadable.append(key)

    if not unreadable:
        configured_count = sum(
            1
            for key in (*_PRIVATE_CA_FILE_ENV_KEYS, *_PRIVATE_CA_DIR_ENV_KEYS)
            if os.environ.get(key)
        )
        return CheckResult(
            name="private CA files",
            passed=True,
            detail=(
                f"{configured_count} configured"
                if configured_count
                else "not configured"
            ),
            fix="",
        )
    return CheckResult(
        name="private CA files",
        passed=False,
        detail=f"unreadable: {', '.join(unreadable)}",
        fix="Unset the variable or point it at a readable certificate file/directory.",
    )


def _check_offline_ttl(
    *, offline: bool, expected_run_duration: str
) -> CheckResult:
    if not offline:
        return CheckResult(
            name="offline credential TTL",
            passed=True,
            detail="not requested",
            fix="",
        )
    from codeprobe.cli.check_infra import run_offline_preflight

    try:
        run_offline_preflight(expected_run_duration, echo=False)
    except DiagnosticError as exc:
        return CheckResult(
            name="offline credential TTL",
            passed=False,
            detail=exc.code,
            fix=exc.message,
        )
    return CheckResult(
        name="offline credential TTL",
        passed=True,
        detail=f">= {expected_run_duration}",
        fix="",
    )


def run_checks(
    agent: str | None = None,
    *,
    offline: bool = False,
    offline_expected_run_duration: str = "1h",
) -> list[CheckResult]:
    """Run all environment checks and return results."""
    selected_agent = _select_agent(agent)
    explicit_agent = agent is not None
    agent_results: list[CheckResult] = []
    for candidate in _DOCTOR_AGENT_ORDER:
        agent_results.extend(
            _agent_path_results(candidate, required=candidate == selected_agent)
        )
    return [
        _check_selected_agent(selected_agent, explicit=explicit_agent),
        *agent_results,
        _check_env_key(
            "ANTHROPIC_API_KEY",
            "Set ANTHROPIC_API_KEY, or sign in to Claude Code with `claude login`.",
            warn_only=True,
        ),
        _check_env_key(
            "OPENAI_API_KEY",
            "Set OPENAI_API_KEY, or sign in with the Codex CLI.",
            warn_only=True,
        ),
        # GitHub is optional (mining matrix treats local paths as
        # first-class), so this check is always advisory — see
        # _check_github_access.
        _check_github_access(),
        _check_git_repo(),
        _check_python_version(),
        _check_container_images(),
        _check_proxy_variables(),
        _check_private_ca_files(),
        _check_offline_ttl(
            offline=offline,
            expected_run_duration=offline_expected_run_duration,
        ),
        _check_user_home_skills(required=selected_agent == "claude"),
    ]


def _any_failed(results: list[CheckResult]) -> bool:
    """True when any NON-advisory check did not pass.

    A ``warn_only`` check that did not pass is advisory (e.g. a missing API
    key whose agent CLI is present) and does not flip doctor to exit-2
    (codeprobe-bgq4).
    """
    return any(not r.passed and not r.warn_only for r in results)


def _llm_available(results: list[CheckResult]) -> bool:
    """Return True when at least one supported agent path is usable."""
    by_name = {r.name: r for r in results}
    claude_ready = (
        by_name.get("claude CLI", CheckResult("", False, "", "")).passed
        and by_name.get("claude auth", CheckResult("", False, "", "")).passed
    )
    copilot_ready = (
        by_name.get("copilot CLI", CheckResult("", False, "", "")).passed
        and by_name.get("copilot auth", CheckResult("", False, "", "")).passed
    )
    return claude_ready or copilot_ready


def _build_compact_envelope(results: list[CheckResult]) -> dict[str, object]:
    """Build a ≤2 KB JSON envelope for SKILL.md preflight substitution."""
    by_name = {r.name: r for r in results}
    gh_auth_ok = by_name.get(
        "GitHub auth", CheckResult("", False, "", "")
    ).passed
    sourcegraph_token_present = any(
        os.environ.get(k) for k in (
            "SOURCEGRAPH_TOKEN", "SRC_ACCESS_TOKEN", "SOURCEGRAPH_ACCESS_TOKEN",
        )
    )
    any_failed = _any_failed(results)

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
    any_failed = _any_failed(results)
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
    existing_data = envelope.get("data")
    envelope["data"] = {
        **(existing_data if isinstance(existing_data, dict) else {}),
        "subsystem_status": subsystem_status,
    }
    envelope["ok"] = not any_failed
    return envelope


@click.command("doctor")
@add_json_flags
@click.option(
    "--compact",
    is_flag=True,
    default=False,
    help=(
        "With --json, emit a minimal envelope (<=2048 bytes) suitable for "
        "SKILL.md `!` substitution. No effect in pretty mode."
    ),
)
@click.option(
    "--agent",
    type=click.Choice(_DOCTOR_AGENT_ORDER, case_sensitive=False),
    default=None,
    help=(
        "Validate one agent path as blocking. Without this, doctor "
        "auto-selects the first usable supported path."
    ),
)
@click.option(
    "--offline",
    is_flag=True,
    default=False,
    help="Also validate offline credential TTL prerequisites.",
)
@click.option(
    "--offline-expected-run-duration",
    "offline_expected_run_duration",
    default="1h",
    show_default=True,
    help="Minimum credential TTL required when --offline is set.",
)
def doctor(
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
    compact: bool,
    agent: str | None,
    offline: bool,
    offline_expected_run_duration: str,
) -> None:
    """Check environment readiness for running codeprobe."""
    mode = resolve_mode(
        "doctor", json_flag, no_json_flag, json_lines_flag,
    )

    results = run_checks(
        agent,
        offline=offline,
        offline_expected_run_duration=offline_expected_run_duration,
    )
    any_failed = _any_failed(results)

    checks_data = {
        "subsystem_status": [asdict(r) for r in results],
        "any_failed": any_failed,
    }

    # --compact path: emit a bounded-size envelope for SKILL.md preflight use.
    # Budget is enforced against the serialised payload; degrade gracefully by
    # dropping verbose fields until we fit.
    if compact and mode.mode != "pretty":
        envelope = _build_compact_envelope(results)
        payload = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
        budget = compact_budget_bytes()
        if len(payload.encode("utf-8")) > budget:
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
            # lint-exempt: compact path bypasses the top-level handler; SystemExit is just the exit code.
            raise SystemExit(1)
        return

    if mode.mode == "pretty":
        for r in results:
            if r.passed:
                click.echo(f"  PASS  {r.name} ({r.detail})")
            elif r.warn_only:
                label = "WARN" if r.fix else "INFO"
                click.echo(f"  {label}  {r.name} ({r.detail})")
                if r.fix:
                    click.echo(f"        -> {r.fix}")
            else:
                click.echo(f"  FAIL  {r.name} ({r.detail})")
                click.echo(f"        -> {r.fix}")
        if any_failed:
            raise DiagnosticError(
                code="DOCTOR_CHECKS_FAILED",
                message="One or more doctor checks failed.",
                diagnose_cmd="codeprobe doctor",
                terminal=True,
                detail={"_envelope_data": checks_data},
            )
        return

    # Envelope / NDJSON mode — let the top-level handler emit the single
    # envelope when checks fail; success still emits a terminal envelope
    # here directly.
    if any_failed:
        raise DiagnosticError(
            code="DOCTOR_CHECKS_FAILED",
            message="One or more doctor checks failed.",
            diagnose_cmd="codeprobe doctor",
            terminal=True,
            detail={"_envelope_data": checks_data},
        )
    emit_envelope(
        command="doctor",
        ok=True,
        exit_code=0,
        data=checks_data,
    )
