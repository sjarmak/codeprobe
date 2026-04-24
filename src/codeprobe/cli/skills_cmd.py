"""``codeprobe skills`` — user-home skill migration helper.

Implements PRD §13-T5 + §16 M-Mod 5: the user-home skills at
``~/.claude/skills/{mine-tasks, run-eval, interpret, check-infra,
calibrate}/`` predate v0.6.0 and now diverge from the authoritative
repo-committed skills at ``.claude/skills/codeprobe-*/SKILL.md`` inside
this repository. Leaving them in place causes Claude Code's skill
resolver to pick the stale copy.

``codeprobe skills migrate`` rewrites each old skill as a tiny
``DEPRECATED`` stub that points at the repo-committed replacement:

* ``user-invocable: false`` — the stub never triggers on its own.
* Description starts with ``DEPRECATED:`` so the skill index is
  explicit.
* Body references the new repo-scoped skill by name so downstream
  agents can follow a migration trail.

Safety rails:

* TTY invocation — caller must confirm the migration (``codeprobe
  skills migrate --yes`` to skip).
* Non-TTY invocation — refuse unless ``CODEPROBE_SKILLS_MIGRATE=ack``
  is set in the environment. This mirrors the fail-loud semantics of
  the tenant / offline gates.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import click

from codeprobe.cli._error_handler import CodeprobeGroup
from codeprobe.cli._output_helpers import add_json_flags, emit_envelope, resolve_mode
from codeprobe.cli.errors import DiagnosticError, PrescriptiveError

__all__ = ["skills"]

# Old user-home skill names → canonical replacement skill name.
USER_HOME_SKILL_MAP: dict[str, str] = {
    "mine-tasks": "codeprobe-mine",
    "run-eval": "codeprobe-run",
    "interpret": "codeprobe-interpret",
    "check-infra": "codeprobe-check-infra",
    "calibrate": "codeprobe-calibrate",
}

_DEPRECATED_BANNER = "DEPRECATED: replaced by"
_DESCRIPTION_TEMPLATE = (
    "{banner} {new_name}. Install the codeprobe PyPI package "
    "(pip install codeprobe) and use the repo-committed skill at "
    ".claude/skills/{new_name}/SKILL.md. This user-home stub exists so "
    "Claude Code's skill resolver no longer picks up the stale copy."
)
_DEPRECATED_TEMPLATE = """\
---
name: {old_name}
description: {description}
user-invocable: false
---

# {old_name} (DEPRECATED)

This skill has moved into the codeprobe repository itself. Use the
authoritative version at ``.claude/skills/{new_name}/SKILL.md``.

## What changed

codeprobe v0.6.0 introduced repo-committed skills that track the CLI's
behaviour release-for-release. The user-home copy here was written
against an older CLI contract and drifts away from the current
envelope / error-code / default-resolution shape.

## What to do

* If you installed codeprobe via ``pip install codeprobe``, the new
  skill ships with the package and your agent will find
  ``codeprobe-{new_suffix}`` automatically inside any project that
  imports the package.
* If you want to keep editing a local copy, delete this directory
  (``rm -r ~/.claude/skills/{old_name}``) and pin the repo-committed
  version via your project-level ``.claude`` config.

Running ``codeprobe skills migrate`` again is idempotent — the stub is
re-written in place.
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillMigrationResult:
    """Outcome of a single per-skill migration step."""

    old_name: str
    new_name: str
    action: str  # "migrated" | "already-deprecated" | "missing" | "skipped"
    path: Path


def _user_skills_root() -> Path:
    """Return ``~/.claude/skills`` (may not exist)."""
    return Path.home() / ".claude" / "skills"


def _is_deprecated_stub(skill_md: Path) -> bool:
    """Return True when ``skill_md`` already contains the DEPRECATED banner."""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return _DEPRECATED_BANNER in text


def _render_stub(old_name: str, new_name: str) -> str:
    suffix = new_name.removeprefix("codeprobe-")
    description = _DESCRIPTION_TEMPLATE.format(
        banner=_DEPRECATED_BANNER,
        new_name=new_name,
    )
    return _DEPRECATED_TEMPLATE.format(
        old_name=old_name,
        new_name=new_name,
        description=description,
        new_suffix=suffix,
    )


def _migrate_one(
    old_name: str,
    new_name: str,
    *,
    user_root: Path,
    write: bool,
) -> SkillMigrationResult:
    """Migrate (or dry-run) a single user-home skill.

    ``write=False`` returns what the migration *would* do without
    touching the filesystem — used by ``--dry-run`` and by tests that
    just want to assert the detection logic.
    """
    skill_dir = user_root / old_name
    skill_md = skill_dir / "SKILL.md"

    if not skill_md.is_file():
        return SkillMigrationResult(old_name, new_name, "missing", skill_md)

    if _is_deprecated_stub(skill_md):
        return SkillMigrationResult(
            old_name, new_name, "already-deprecated", skill_md
        )

    if not write:
        return SkillMigrationResult(old_name, new_name, "skipped", skill_md)

    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md.write_text(_render_stub(old_name, new_name), encoding="utf-8")
    return SkillMigrationResult(old_name, new_name, "migrated", skill_md)


def scan_user_home_skills(
    user_root: Path | None = None,
) -> list[SkillMigrationResult]:
    """Return per-skill detection results without writing anything.

    Used by :mod:`codeprobe.cli.doctor_cmd` to emit the
    ``STALE_USER_HOME_SKILL`` diagnostic — a stale skill is one whose
    ``SKILL.md`` exists and does NOT yet carry the deprecation banner.
    """
    root = user_root if user_root is not None else _user_skills_root()
    return [
        _migrate_one(old, new, user_root=root, write=False)
        for old, new in USER_HOME_SKILL_MAP.items()
    ]


def stale_user_home_skills(
    user_root: Path | None = None,
) -> list[SkillMigrationResult]:
    """Return only the entries that still need migration."""
    return [r for r in scan_user_home_skills(user_root) if r.action == "skipped"]


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


_NON_TTY_ACK_ENV = "CODEPROBE_SKILLS_MIGRATE"
_NON_TTY_ACK_VALUE = "ack"


@click.group(cls=CodeprobeGroup)
def skills() -> None:
    """Manage Claude Code skill surfaces for codeprobe."""


@skills.command("migrate")
@add_json_flags
@click.option(
    "--yes",
    "yes_flag",
    is_flag=True,
    default=False,
    help="Skip the interactive confirmation prompt.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report what would change without touching the filesystem.",
)
def migrate_cmd(
    yes_flag: bool,
    dry_run: bool,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Rewrite stale user-home codeprobe skills as deprecation stubs.

    On a TTY the command prompts before writing unless ``--yes`` is
    passed.  When stdout is not a TTY (CI, wrappers) the command
    refuses unless ``CODEPROBE_SKILLS_MIGRATE=ack`` is set in the
    environment so skill state is never rewritten without an explicit
    acknowledgement.

    The migration is idempotent: re-running against already-deprecated
    skills is a no-op and exits 0.
    """
    out_mode = resolve_mode(
        "skills migrate", json_flag, no_json_flag, json_lines_flag,
    )
    user_root = _user_skills_root()
    scan = scan_user_home_skills(user_root)
    pending = [r for r in scan if r.action == "skipped"]

    if dry_run or not pending:
        emit_envelope(
            command="skills migrate",
            data={
                "dry_run": dry_run,
                "user_root": str(user_root),
                "results": [
                    {
                        "old_name": r.old_name,
                        "new_name": r.new_name,
                        "action": r.action,
                        "path": str(r.path),
                    }
                    for r in scan
                ],
                "pending_count": len(pending),
            },
        )
        if out_mode.mode == "pretty":
            for r in scan:
                click.echo(f"  {r.action:>20s}  {r.old_name} → {r.new_name}")
        return

    # Write path: require explicit ack.
    if not _has_write_acknowledgement(yes_flag):
        raise PrescriptiveError(
            code="SOURCE_EXPORT_REQUIRES_ACK",
            message=(
                "codeprobe skills migrate would rewrite "
                f"{len(pending)} user-home skill file(s) under {user_root}. "
                "Re-run with --yes on a TTY, or set "
                f"{_NON_TTY_ACK_ENV}={_NON_TTY_ACK_VALUE} in CI."
            ),
            next_try_flag="--yes",
            next_try_value="",
            detail={
                "pending": [r.old_name for r in pending],
                "non_tty_ack_env": _NON_TTY_ACK_ENV,
            },
        )

    results = [
        _migrate_one(r.old_name, r.new_name, user_root=user_root, write=True)
        for r in scan
    ]

    emit_envelope(
        command="skills migrate",
        data={
            "dry_run": False,
            "user_root": str(user_root),
            "results": [
                {
                    "old_name": r.old_name,
                    "new_name": r.new_name,
                    "action": r.action,
                    "path": str(r.path),
                }
                for r in results
            ],
            "pending_count": sum(
                1 for r in results if r.action == "skipped"
            ),
        },
    )
    if out_mode.mode == "pretty":
        for r in results:
            click.echo(f"  {r.action:>20s}  {r.old_name} → {r.new_name}")


def _has_write_acknowledgement(yes_flag: bool) -> bool:
    """Return True when the caller has opted into writing the stubs.

    * ``--yes`` on any invocation short-circuits.
    * On a TTY, prompt.
    * On non-TTY, require ``CODEPROBE_SKILLS_MIGRATE=ack``.
    """
    if yes_flag:
        return True

    if sys.stdin.isatty():
        try:
            return click.confirm(
                "Rewrite user-home codeprobe skills as deprecation stubs?",
                default=False,
            )
        except click.exceptions.Abort:
            return False

    return (
        os.environ.get(_NON_TTY_ACK_ENV, "").strip() == _NON_TTY_ACK_VALUE
    )


def check_stale_user_home_skills_or_raise() -> None:
    """Raise :class:`DiagnosticError` ``STALE_USER_HOME_SKILL`` on drift.

    Called from :mod:`codeprobe.cli.doctor_cmd`. No-op when nothing is
    stale so the doctor check cost is a single directory stat per known
    skill name.
    """
    stale = stale_user_home_skills()
    if not stale:
        return
    raise DiagnosticError(
        code="STALE_USER_HOME_SKILL",
        message=(
            f"{len(stale)} user-home codeprobe skill(s) predate the "
            "repo-committed skills and have not been migrated. Claude "
            "Code's skill resolver may pick the stale copy over the "
            "authoritative one."
        ),
        diagnose_cmd="codeprobe skills migrate --dry-run",
        terminal=False,
        next_steps=[
            (
                "Preview the migration without writing",
                "codeprobe skills migrate --dry-run",
            ),
            (
                "Run the migration (prompts on TTY, requires "
                f"{_NON_TTY_ACK_ENV}={_NON_TTY_ACK_VALUE} in CI)",
                "codeprobe skills migrate --yes",
            ),
        ],
        detail={
            "stale_skills": [r.old_name for r in stale],
        },
    )
