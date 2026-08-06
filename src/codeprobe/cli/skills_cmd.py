"""``codeprobe skills`` — skill installation and migration helpers.

``codeprobe skills install`` copies the packaged agent skills
(``codeprobe.skills_data`` wheel package data) into a ``.claude/skills``
directory — ``~/.claude/skills`` by default, the project-local one with
``--project`` — so pip customers get the paired-skills contract (PRD §7)
without cloning this repository. Existing files that differ from the
packaged versions are never overwritten without ``--force``.

The user-home default follows from the CLI's own shape: the repository
under test is an argument to ``codeprobe mine`` / ``codeprobe run``, so
the skills are not tied to the directory they were installed from, and
scoping them per-project would mean re-installing for every repo someone
wants to benchmark.

``codeprobe skills migrate`` implements PRD §13-T5 + §16 M-Mod 5: the
user-home skills at
``~/.claude/skills/{mine-tasks, run-eval, interpret, check-infra,
calibrate}/`` predate v0.6.0 and now diverge from the authoritative
packaged skills. Leaving them in place causes Claude Code's skill
resolver to pick the stale copy. The migration rewrites each old skill
as a tiny ``DEPRECATED`` stub that points at the replacement:

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
from importlib import resources
from pathlib import Path

import click

from codeprobe import __version__
from codeprobe.cli._banner import render_banner, should_print_banner
from codeprobe.cli._error_handler import CodeprobeGroup
from codeprobe.cli._output_helpers import add_json_flags, emit_envelope, resolve_mode
from codeprobe.cli.errors import DiagnosticError, PrescriptiveError

__all__ = ["skills"]

# Written at the root of the destination skills tree so a later
# ``pip install -U codeprobe`` can be detected as skill drift: the copied
# SKILL.md files are inert data and never update themselves.
_VERSION_STAMP = ".codeprobe_version"

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

* If you installed codeprobe via ``pip install codeprobe``, run
  ``codeprobe skills install`` to materialize the packaged
  ``codeprobe-{new_suffix}`` replacement into ``~/.claude/skills/``
  (or pass ``--project`` to scope it to the current repository).
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


@dataclass(frozen=True)
class SkillInstallPlan:
    """Planned action for a single packaged skill during ``install``."""

    name: str
    action: str  # "installed" | "unchanged" | "overwritten"
    target: Path
    content: str


def _packaged_skills() -> list[tuple[str, str]]:
    """Return ``(skill_name, SKILL.md text)`` for every packaged skill.

    Enumerates ``codeprobe.skills_data`` wheel package data. The
    ``codeprobe-*`` filter is defensive: the package should contain
    nothing else, but a stray directory must never be installed into a
    customer's ``.claude/skills``.
    """
    root = resources.files("codeprobe.skills_data")
    found: list[tuple[str, str]] = []
    for entry in root.iterdir():
        if not entry.name.startswith("codeprobe-") or not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.is_file():
            continue
        found.append((entry.name, skill_md.read_text(encoding="utf-8")))
    return sorted(found)


def _plan_skill_install(
    packaged: list[tuple[str, str]], dest: Path, *, force: bool
) -> tuple[list[SkillInstallPlan], list[str]]:
    """Plan the per-skill actions without touching the filesystem.

    Returns ``(plan, conflicts)``; a non-empty ``conflicts`` list means
    the caller must refuse before any write happens.
    """
    plan: list[SkillInstallPlan] = []
    conflicts: list[str] = []
    for name, content in packaged:
        target = dest / name / "SKILL.md"
        if not target.is_file():
            plan.append(SkillInstallPlan(name, "installed", target, content))
        elif target.read_bytes() == content.encode("utf-8"):
            plan.append(SkillInstallPlan(name, "unchanged", target, content))
        elif force:
            plan.append(SkillInstallPlan(name, "overwritten", target, content))
        else:
            conflicts.append(name)
    return plan, conflicts


@dataclass(frozen=True)
class SkillVersionDrift:
    """A destination whose installed skills predate (or postdate) the CLI."""

    dest: Path
    stamped: str
    package: str
    direction: str  # "behind" | "ahead"


def _stamp_path(dest: Path) -> Path:
    return dest / _VERSION_STAMP


def _write_version_stamp(dest: Path, version: str) -> Path:
    """Record which codeprobe version materialized the skills in ``dest``."""
    target = _stamp_path(dest)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"{version}\n", encoding="utf-8")
    return target


def read_version_stamp(dest: Path) -> str | None:
    """Return the stamped version for ``dest``, or None when unstamped.

    An unreadable or empty stamp is reported as absent rather than as an
    error: the stamp is advisory metadata, and a skill tree installed by
    a pre-stamp codeprobe is a legitimate state.
    """
    try:
        text = _stamp_path(dest).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text.strip() or None


def _version_tuple(raw: str) -> tuple[int, ...]:
    """Parse the leading numeric release components of a version string.

    ``"0.14.0rc2"`` → ``(0, 14, 0)``. Pre-release suffixes are dropped,
    so they compare equal to their release — which is why callers treat
    an equal tuple with unequal text as "behind" (re-run install) rather
    than guessing a direction.
    """
    parts: list[int] = []
    for chunk in raw.split(".")[:3]:
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def skill_version_drift(
    dest: Path, package_version: str | None = None
) -> SkillVersionDrift | None:
    """Compare ``dest``'s version stamp against the running package.

    Returns None when the stamp is absent (nothing installed here, or
    installed before stamping existed) or matches. ``direction`` is
    ``"ahead"`` only when the stamp parses as a strictly newer release —
    that case must NOT advise re-running install, which would silently
    downgrade the skills.
    """
    package = package_version if package_version is not None else __version__
    stamped = read_version_stamp(dest)
    if stamped is None or stamped == package:
        return None
    direction = (
        "ahead" if _version_tuple(stamped) > _version_tuple(package) else "behind"
    )
    return SkillVersionDrift(
        dest=dest, stamped=stamped, package=package, direction=direction
    )


def installed_skill_drift(
    roots: list[Path] | None = None, package_version: str | None = None
) -> list[SkillVersionDrift]:
    """Report drift across the two destinations ``install`` writes to.

    Defaults to the project-local ``./.claude/skills`` and the user-home
    ``~/.claude/skills`` — the ``--dest`` escape hatch is deliberately
    not tracked, since codeprobe has no way to discover those paths later.
    """
    candidates = (
        roots
        if roots is not None
        else [Path.cwd() / ".claude" / "skills", _user_skills_root()]
    )
    found = [skill_version_drift(root, package_version) for root in candidates]
    return [drift for drift in found if drift is not None]


def _next_steps(dest: Path, count: int) -> list[str]:
    """Lines telling the operator what to do with the skills just copied.

    The skills carry ``user-invocable: false`` — they are matched by
    description, not typed as slash commands — so the call to action is
    phrased as a request to the agent, not a command to run.
    """
    lines = [
        "",
        f"Done — {count} skill(s) at {dest}",
        "",
        "Open Claude Code and ask for what you want. The repository to mine is an",
        "argument, so it can be the one you are in or any other path:",
        '  "mine 5 eval tasks from this repo"',
        '  "mine eval tasks from ~/src/myproject and run them against claude"',
        '  "interpret the last run"',
        "",
    ]
    if dest == _user_skills_root():
        lines.append(
            "Available in every project on this machine; they are model-invoked, "
            "not slash commands."
        )
    elif dest.name == "skills" and dest.parent.name == ".claude":
        lines.append(
            "A new Claude Code session in this project picks these up; they are "
            "model-invoked, not slash commands. Use --user to make them available "
            "everywhere."
        )
    else:
        lines.append(
            "Note: Claude Code only loads skills from ./.claude/skills or "
            "~/.claude/skills — copy this directory into one of those to "
            "activate them."
        )
    return lines


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


@skills.command("install")
@add_json_flags
@click.option(
    "--dest",
    "dest_opt",
    type=click.Path(path_type=Path),
    default=None,
    help="Directory to install skills into (default: ~/.claude/skills).",
)
@click.option(
    "--user",
    "user_flag",
    is_flag=True,
    default=False,
    help="Install into ~/.claude/skills — the default, stated explicitly.",
)
@click.option(
    "--project",
    "project_flag",
    is_flag=True,
    default=False,
    help="Install into ./.claude/skills of the current directory instead.",
)
@click.option(
    "--force",
    "force_flag",
    is_flag=True,
    default=False,
    help="Overwrite existing SKILL.md files that differ from the packaged versions.",
)
def install_cmd(
    dest_opt: Path | None,
    user_flag: bool,
    project_flag: bool,
    force_flag: bool,
    json_flag: bool,
    no_json_flag: bool,
    json_lines_flag: bool,
) -> None:
    """Install the packaged codeprobe agent skills into .claude/skills.

    Copies every ``codeprobe-*/SKILL.md`` shipped inside the
    ``codeprobe`` wheel (package data under ``codeprobe.skills_data``)
    into the destination. Files already identical to the packaged
    versions are left untouched; files that differ are never overwritten
    without ``--force`` — the command refuses with
    ``SKILL_INSTALL_CONFLICT`` before writing anything.

    The default is ``~/.claude/skills``: the repository being benchmarked
    is an argument to ``codeprobe mine`` / ``run``, not the directory you
    installed from, so scoping the skills to one project would make the
    common case (mine a path from wherever you happen to be) fail. Use
    ``--project`` to scope them to the current repository instead.
    """
    out_mode = resolve_mode(
        "skills install", json_flag, no_json_flag, json_lines_flag,
    )
    selectors = [
        name
        for name, chosen in (
            ("--dest", dest_opt is not None),
            ("--user", user_flag),
            ("--project", project_flag),
        )
        if chosen
    ]
    if len(selectors) > 1:
        raise PrescriptiveError(
            code="MUTEX_FLAGS",
            message=(
                f"Cannot combine {' with '.join(selectors)}. Pick one "
                "destination: --user for ~/.claude/skills (the default), "
                "--project for ./.claude/skills, or --dest <path>."
            ),
            next_try_flag=selectors[0],
            next_try_value="",
            detail={"conflicting_flags": selectors},
        )
    if project_flag:
        dest = Path.cwd() / ".claude" / "skills"
    elif dest_opt is not None:
        dest = dest_opt
    else:
        dest = _user_skills_root()

    plan, conflicts = _plan_skill_install(
        _packaged_skills(), dest, force=force_flag
    )
    if conflicts:
        raise PrescriptiveError(
            code="SKILL_INSTALL_CONFLICT",
            message=(
                f"{len(conflicts)} skill file(s) under {dest} differ from "
                "the packaged versions; refusing to overwrite local edits "
                "without --force. Nothing was written."
            ),
            next_try_flag="--force",
            next_try_value="",
            detail={"dest": str(dest), "conflicts": conflicts},
        )

    for step in plan:
        if step.action != "unchanged":
            step.target.parent.mkdir(parents=True, exist_ok=True)
            step.target.write_text(step.content, encoding="utf-8")

    # Stamped on every successful install, including the all-unchanged
    # re-run: that is exactly the path an upgraded package takes to clear
    # its own drift warning.
    stamp = _write_version_stamp(dest, __version__)

    emit_envelope(
        command="skills install",
        data={
            "dest": str(dest),
            "installed": [s.name for s in plan if s.action == "installed"],
            "unchanged": [s.name for s in plan if s.action == "unchanged"],
            "conflicts_overwritten": [
                s.name for s in plan if s.action == "overwritten"
            ],
            "skill_count": len(plan),
            "version": __version__,
            "stamp_path": str(stamp),
        },
    )
    if out_mode.mode == "pretty":
        # Printed after the envelope, not before it, so the human-facing
        # block (banner → what was copied → what to do next) stays
        # contiguous at the end of the output instead of being split by
        # the machine-readable line that every command also emits.
        if should_print_banner(mode=out_mode.mode, use_rich=out_mode.use_rich):
            click.echo(render_banner(__version__))
        for step in plan:
            click.echo(f"  {step.action:>20s}  {step.name} → {step.target}")
        for line in _next_steps(dest, len(plan)):
            click.echo(line)


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
