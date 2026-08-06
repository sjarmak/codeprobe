"""Doctor command — checks environment readiness for codeprobe."""

from __future__ import annotations

import json
import shutil as shutil
import subprocess
import sys
from dataclasses import asdict
from typing import TYPE_CHECKING

import click

from codeprobe import __version__
from codeprobe.cli import doctor_env
from codeprobe.cli._output_helpers import (
    add_json_flags,
    emit_envelope,
    resolve_mode,
)
from codeprobe.cli.doctor_agents import (
    _DOCTOR_AGENT_ORDER,
    CheckResult,
    _agent_path_results,
    _check_github_access,
    _check_selected_agent,
    _optional_result,
    _select_agent,
)
from codeprobe.cli.doctor_agents import (
    _check_copilot_auth as _check_copilot_auth,
)
from codeprobe.cli.errors import DiagnosticError
from codeprobe.config.defaults import compact_budget_bytes

if TYPE_CHECKING:
    # Imported for typing only; the runtime import stays inside the check
    # function so `codeprobe doctor` does not pay for it on every command.
    from codeprobe.cli.skills_cmd import SkillDriftReport

_COPILOT_OFFLINE_ENV_KEYS: tuple[str, ...] = (
    "COPILOT_PROVIDER_BASE_URL",
    "COPILOT_MODEL",
)
_CONTAINER_BOOTSTRAP_FIX = "Run 'codeprobe bootstrap'."
_env_has_value = doctor_env.env_has_value


def _check_env_key(key: str, fix: str, *, warn_only: bool = False) -> CheckResult:
    present = _env_has_value(key)
    return CheckResult(
        name=key,
        passed=present,
        detail="set" if present else "not set",
        fix=fix,
        warn_only=warn_only and not present,
    )


def _check_git_repo(repo: str = ".") -> CheckResult:
    try:
        result = subprocess.run(
            ["git", "-C", repo.strip() or ".", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        is_repo = result.returncode == 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
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


def _check_go_ast_toolchain() -> CheckResult:
    from codeprobe.mining.ast_resolver import go_toolchain_status

    passed, detail = go_toolchain_status()
    return CheckResult(
        name="Go AST scanner",
        passed=passed,
        detail=detail,
        fix=(
            "" if passed else "Install Go 1.24 or newer to mine Go AST evidence."
        ),
        warn_only=not passed,
    )


def _check_install_provenance() -> CheckResult:
    """Flag a codeprobe that is bound to a foreign source tree (codeprobe-v3wn).

    Catches the shared-``.venv`` failure where an editable install run from a
    git worktree repoints the project-root CLI's ``.pth``/console-script at
    that worktree, so ``codeprobe`` silently executes stale code.
    """
    import codeprobe
    from codeprobe import provenance

    report = provenance.analyze(
        package_file=codeprobe.__file__,
        prefix=sys.prefix,
        argv0=sys.argv[0] if sys.argv else "",
    )
    return CheckResult(
        name="install provenance",
        passed=report.ok,
        detail=report.detail,
        fix=report.fix,
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


def _check_installed_skill_version() -> CheckResult:
    """Flag installed skills left behind by a package upgrade (codeprobe-ybw7).

    Always advisory: stale skills degrade agent guidance but do not stop
    the CLI, and an unstamped tree (installed before stamping, or never
    installed) is a pass.
    """
    from codeprobe.cli.skills_cmd import installed_skill_drift

    drift = installed_skill_drift()
    if not drift:
        return CheckResult(
            name="installed skills up to date",
            passed=True,
            detail="~/.claude/skills and ./.claude/skills match the packaged skills",
            fix="",
            warn_only=True,
        )
    detail = "; ".join(_describe_skill_drift(d) for d in drift)
    if any(d.version_direction == "ahead" for d in drift):
        # Re-running install against a newer skill tree would overwrite it
        # with the older packaged copy, so the fix is to upgrade instead.
        fix = (
            "Run 'pip install -U codeprobe' — the skills are newer than the "
            "CLI, and 'codeprobe skills install' would downgrade them."
        )
    elif any(d.stale for d in drift):
        # Content differs at the same version: either a hand edit or a
        # source tree that moved ahead of an installed copy. Install
        # refuses to clobber edits without --force, so name both.
        fix = (
            "Run 'codeprobe skills install' to refresh them; if it reports "
            "SKILL_INSTALL_CONFLICT because you edited a copy, re-run with "
            "--force to take the packaged version."
        )
    else:
        fix = "Run 'codeprobe skills install' to refresh them."
    return CheckResult(
        name="installed skills up to date",
        passed=False,
        detail=detail,
        fix=fix,
        warn_only=True,
    )


def _describe_skill_drift(report: SkillDriftReport) -> str:
    """One-line summary of a drift report, most severe part first."""
    parts: list[str] = []
    if report.version_direction:
        parts.append(f"stamped {report.stamped}, CLI is {report.package}")
    if report.stale:
        parts.append(
            f"{len(report.stale)} skill(s) differ from the packaged copy "
            f"({', '.join(report.stale)})"
        )
    if report.missing:
        parts.append(
            f"{len(report.missing)} not installed ({', '.join(report.missing)})"
        )
    return f"{report.dest}: " + "; ".join(parts)


def _check_container_images(*, required: bool) -> CheckResult:
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
            fix="Install Docker or Podman, or run from an already sandboxed environment.",
            warn_only=not required,
        )

    try:
        missing_count = _missing_container_image_count(engine)
    except ValueError as exc:
        return CheckResult(
            name="container images",
            passed=False,
            detail=f"image configuration invalid: {exc}",
            fix=container_runner.image_configuration_remediation(),
            warn_only=not required,
        )
    if missing_count == 0:
        return CheckResult(
            name="container images",
            passed=True,
            detail="agent and scoring images available",
            fix="",
        )
    return CheckResult(
        name="container images",
        passed=False,
        detail=f"{missing_count} required image(s) missing",
        fix=_CONTAINER_BOOTSTRAP_FIX,
        warn_only=not required,
    )


def _missing_container_image_count(engine: str) -> int:
    from codeprobe.sandbox import runner as container_runner

    required_images = (
        container_runner.agent_image_reference(),
        container_runner.scoring_image_reference(),
    )
    return sum(
        1
        for image in required_images
        if not container_runner.image_available(engine, image)
    )


def _check_proxy_variables() -> CheckResult:
    passed, detail = doctor_env.proxy_variables_status()
    if not passed:
        return CheckResult(
            name="proxy variables",
            passed=False,
            detail=detail,
            fix=(
                "Set proxy variables to valid proxy URLs, or unset them. "
                "Use comma-separated hosts for NO_PROXY."
            ),
        )
    return CheckResult(
        name="proxy variables",
        passed=True,
        detail=detail,
        fix="",
    )


def _check_private_ca_files(
    private_ca_paths: tuple[str, ...] = (),
) -> CheckResult:
    passed, detail = doctor_env.private_ca_files_status(private_ca_paths)
    if passed:
        return CheckResult(
            name="private CA files",
            passed=True,
            detail=detail,
            fix="",
        )
    return CheckResult(
        name="private CA files",
        passed=False,
        detail=detail,
        fix="Unset the variable or point it at a readable certificate file/directory.",
    )


def _check_copilot_offline_prerequisites() -> CheckResult:
    missing: list[str] = []
    if doctor_env.env_value("COPILOT_OFFLINE").lower() != "true":
        missing.append("COPILOT_OFFLINE=true")
    missing.extend(key for key in _COPILOT_OFFLINE_ENV_KEYS if not _env_has_value(key))
    if missing:
        return CheckResult(
            name="offline credential TTL",
            passed=False,
            detail="copilot BYOK offline prerequisites missing",
            fix=(
                "Set "
                + ", ".join(missing)
                + " before running Copilot CLI with --offline."
            ),
        )
    return CheckResult(
        name="offline credential TTL",
        passed=True,
        detail="copilot BYOK offline configured",
        fix="",
    )


def _check_offline_ttl(
    *, offline: bool, expected_run_duration: str, selected_agent: str | None
) -> CheckResult:
    if not offline:
        return CheckResult(
            name="offline credential TTL",
            passed=True,
            detail="not requested",
            fix="",
        )
    if selected_agent == "copilot":
        return _check_copilot_offline_prerequisites()
    backend_filter = doctor_env.offline_backends_for_agent(selected_agent)
    if selected_agent is not None and not backend_filter:
        return CheckResult(
            name="offline credential TTL",
            passed=True,
            detail=f"not applicable for {selected_agent}",
            fix="",
        )
    from codeprobe.cli.check_infra import run_offline_preflight

    try:
        run_offline_preflight(
            expected_run_duration,
            backend_filter=backend_filter,
            echo=False,
        )
    except DiagnosticError as exc:
        return CheckResult(
            name="offline credential TTL",
            passed=False,
            detail=exc.code,
            fix=f"Run `{exc.diagnose_cmd}` to inspect credential TTLs.",
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
    repo: str = ".",
    private_ca: tuple[str, ...] = (),
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
        _check_git_repo(repo),
        _check_python_version(),
        _check_go_ast_toolchain(),
        _check_install_provenance(),
        _check_container_images(required=selected_agent is not None),
        _check_proxy_variables(),
        _check_private_ca_files(private_ca),
        _check_offline_ttl(
            offline=offline,
            expected_run_duration=offline_expected_run_duration,
            selected_agent=selected_agent,
        ),
        _check_user_home_skills(required=selected_agent == "claude"),
        _check_installed_skill_version(),
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
        _env_has_value(k) for k in (
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


def _checks_data(
    results: list[CheckResult], *, any_failed: bool
) -> dict[str, object]:
    return {
        "subsystem_status": [asdict(result) for result in results],
        "any_failed": any_failed,
    }


def _emit_compact(results: list[CheckResult], *, any_failed: bool) -> None:
    envelope = _build_compact_envelope(results)
    payload = json.dumps(envelope, sort_keys=True, separators=(",", ":"))
    if len(payload.encode("utf-8")) > compact_budget_bytes():
        payload = json.dumps(
            {
                "record_type": "doctor",
                "ok": not any_failed,
                "command": "doctor",
                "version": envelope["version"],
                "schema_version": 1,
                "exit_code": 1 if any_failed else 0,
                "error": None,
                "data": envelope["data"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    click.echo(payload)
    if any_failed:
        # lint-exempt: compact mode emits directly and SystemExit is its exit code.
        raise SystemExit(1)


def _emit_pretty(
    results: list[CheckResult],
    *,
    any_failed: bool,
    checks_data: dict[str, object],
) -> None:
    for result in results:
        if result.passed and result.warn_only:
            click.echo(f"  INFO  {result.name} ({result.detail})")
        elif result.passed:
            click.echo(f"  PASS  {result.name} ({result.detail})")
        elif result.warn_only:
            label = "WARN" if result.fix else "INFO"
            click.echo(f"  {label}  {result.name} ({result.detail})")
            if result.fix:
                click.echo(f"        -> {result.fix}")
        else:
            click.echo(f"  FAIL  {result.name} ({result.detail})")
            click.echo(f"        -> {result.fix}")
    if any_failed:
        _raise_doctor_failure(checks_data)


def _raise_doctor_failure(checks_data: dict[str, object]) -> None:
    raise DiagnosticError(
        code="DOCTOR_CHECKS_FAILED",
        message="One or more doctor checks failed.",
        diagnose_cmd="codeprobe doctor",
        terminal=True,
        detail={"_envelope_data": checks_data},
    )


def _emit_structured(
    checks_data: dict[str, object], *, any_failed: bool
) -> None:
    if any_failed:
        _raise_doctor_failure(checks_data)
    emit_envelope(
        command="doctor",
        ok=True,
        exit_code=0,
        data=checks_data,
    )


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
    "--repo",
    default=".",
    show_default=True,
    help="Repository path whose git readiness should be validated.",
)
@click.option(
    "--private-ca",
    "private_ca",
    multiple=True,
    metavar="PATH",
    help="Private CA certificate file to validate. Can be passed more than once.",
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
    repo: str,
    private_ca: tuple[str, ...],
    offline: bool,
    offline_expected_run_duration: str,
) -> None:
    """Check environment readiness for running codeprobe."""
    mode = resolve_mode(
        "doctor", json_flag, no_json_flag, json_lines_flag,
    )

    results = run_checks(
        agent,
        repo=repo,
        private_ca=private_ca,
        offline=offline,
        offline_expected_run_duration=offline_expected_run_duration,
    )
    any_failed = _any_failed(results)
    checks_data = _checks_data(results, any_failed=any_failed)
    if compact and mode.mode != "pretty":
        _emit_compact(results, any_failed=any_failed)
        return
    if mode.mode == "pretty":
        _emit_pretty(
            results,
            any_failed=any_failed,
            checks_data=checks_data,
        )
        return
    _emit_structured(checks_data, any_failed=any_failed)
