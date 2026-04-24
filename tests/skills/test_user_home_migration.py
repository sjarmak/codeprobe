"""Tests for the ``codeprobe skills migrate`` helper.

Exercises the TTY / non-TTY / idempotency paths outlined in PRD §13-T5
+ §16 M-Mod 5 + bead ``codeprobe-coa``.

The tests redirect ``HOME`` into ``tmp_path`` via monkeypatch so the
real user-home skill tree is never touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli.skills_cmd import (
    USER_HOME_SKILL_MAP,
    _is_deprecated_stub,
    _render_stub,
    scan_user_home_skills,
    skills,
    stale_user_home_skills,
)


@pytest.fixture
def fake_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Redirect ``Path.home()`` into tmp_path and clear the write-ack env."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEPROBE_SKILLS_MIGRATE", raising=False)
    return tmp_path


def _plant_old_skill(home: Path, name: str) -> Path:
    """Create a fake old-style user-home skill at ``home/.claude/skills/<name>``."""
    skill_dir = home / ".claude" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {name}\ndescription: legacy\n---\n# {name}\n",
        encoding="utf-8",
    )
    return skill_md


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def test_scan_reports_missing_when_no_skill_tree(fake_home: Path) -> None:
    results = scan_user_home_skills()
    assert all(r.action == "missing" for r in results)
    assert {r.old_name for r in results} == set(USER_HOME_SKILL_MAP)


def test_scan_reports_skipped_for_legacy_skill(fake_home: Path) -> None:
    _plant_old_skill(fake_home, "mine-tasks")
    results = {r.old_name: r for r in scan_user_home_skills()}
    assert results["mine-tasks"].action == "skipped"
    # Untouched skills still report as missing.
    assert results["run-eval"].action == "missing"


def test_stale_filter_returns_only_pending_entries(fake_home: Path) -> None:
    _plant_old_skill(fake_home, "mine-tasks")
    _plant_old_skill(fake_home, "interpret")
    stale = stale_user_home_skills()
    assert sorted(r.old_name for r in stale) == ["interpret", "mine-tasks"]


def test_is_deprecated_stub_detects_banner(tmp_path: Path) -> None:
    """``_is_deprecated_stub`` only matches the explicit DEPRECATED banner."""
    skill_md = tmp_path / "SKILL.md"

    skill_md.write_text(_render_stub("mine-tasks", "codeprobe-mine"))
    assert _is_deprecated_stub(skill_md) is True

    skill_md.write_text("---\nname: foo\n---\n")
    assert _is_deprecated_stub(skill_md) is False


# ---------------------------------------------------------------------------
# CLI — TTY path (prompt + --yes)
# ---------------------------------------------------------------------------


def test_cli_dry_run_reports_without_writing(fake_home: Path) -> None:
    _plant_old_skill(fake_home, "mine-tasks")
    _plant_old_skill(fake_home, "run-eval")

    runner = CliRunner()
    result = runner.invoke(
        skills,
        ["migrate", "--dry-run", "--json"],
        env={"HOME": str(fake_home)},
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[-1])
    assert payload["data"]["pending_count"] == 2
    # Bodies untouched.
    for name in ("mine-tasks", "run-eval"):
        body = (fake_home / ".claude" / "skills" / name / "SKILL.md").read_text()
        assert "DEPRECATED" not in body


def test_cli_yes_flag_writes_stubs(fake_home: Path) -> None:
    _plant_old_skill(fake_home, "mine-tasks")
    _plant_old_skill(fake_home, "calibrate")

    runner = CliRunner()
    result = runner.invoke(
        skills,
        ["migrate", "--yes", "--json"],
        env={"HOME": str(fake_home)},
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.splitlines()[-1])
    assert payload["data"]["pending_count"] == 0

    for old_name, new_name in USER_HOME_SKILL_MAP.items():
        if old_name not in ("mine-tasks", "calibrate"):
            continue
        skill_md = fake_home / ".claude" / "skills" / old_name / "SKILL.md"
        text = skill_md.read_text()
        assert "DEPRECATED" in text
        assert new_name in text


def test_cli_is_idempotent(fake_home: Path) -> None:
    """Re-running after a successful migration is a no-op."""
    _plant_old_skill(fake_home, "mine-tasks")
    runner = CliRunner()
    first = runner.invoke(
        skills, ["migrate", "--yes", "--json"], env={"HOME": str(fake_home)}
    )
    assert first.exit_code == 0

    second = runner.invoke(
        skills, ["migrate", "--yes", "--json"], env={"HOME": str(fake_home)}
    )
    assert second.exit_code == 0
    payload = json.loads(second.output.splitlines()[-1])
    # Now reported as already-deprecated rather than migrated.
    for entry in payload["data"]["results"]:
        if entry["old_name"] == "mine-tasks":
            assert entry["action"] == "already-deprecated"


# ---------------------------------------------------------------------------
# CLI — non-TTY path (env-ack gate)
# ---------------------------------------------------------------------------


def test_cli_non_tty_without_ack_raises_prescriptive(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-TTY invocation without ``CODEPROBE_SKILLS_MIGRATE=ack`` refuses."""
    _plant_old_skill(fake_home, "mine-tasks")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.delenv("CODEPROBE_SKILLS_MIGRATE", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        skills,
        ["migrate", "--json"],
        env={"HOME": str(fake_home)},
    )
    # Prescriptive error → exit 2; envelope body includes remediation.
    assert result.exit_code != 0, result.output
    payload = json.loads(result.output.splitlines()[-1])
    assert payload["ok"] is False
    assert payload["error"]["code"] == "SOURCE_EXPORT_REQUIRES_ACK"


def test_cli_non_tty_with_ack_writes(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _plant_old_skill(fake_home, "mine-tasks")
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    runner = CliRunner()
    result = runner.invoke(
        skills,
        ["migrate", "--json"],
        env={
            "HOME": str(fake_home),
            "CODEPROBE_SKILLS_MIGRATE": "ack",
        },
    )
    assert result.exit_code == 0, result.output
    skill_md = fake_home / ".claude" / "skills" / "mine-tasks" / "SKILL.md"
    assert "DEPRECATED" in skill_md.read_text()


# ---------------------------------------------------------------------------
# Doctor integration — STALE_USER_HOME_SKILL emission
# ---------------------------------------------------------------------------


def test_doctor_check_flags_stale_skill(fake_home: Path) -> None:
    from codeprobe.cli.doctor_cmd import _check_user_home_skills

    _plant_old_skill(fake_home, "run-eval")
    result = _check_user_home_skills()
    assert result.passed is False
    assert "run-eval" in result.detail
    assert "codeprobe skills migrate" in result.fix


def test_doctor_check_passes_when_no_stale_skills(fake_home: Path) -> None:
    from codeprobe.cli.doctor_cmd import _check_user_home_skills

    result = _check_user_home_skills()
    assert result.passed is True


def test_stale_check_raises_diagnostic_error(fake_home: Path) -> None:
    from codeprobe.cli.errors import DiagnosticError
    from codeprobe.cli.skills_cmd import check_stale_user_home_skills_or_raise

    _plant_old_skill(fake_home, "interpret")
    with pytest.raises(DiagnosticError) as exc_info:
        check_stale_user_home_skills_or_raise()
    assert exc_info.value.code == "STALE_USER_HOME_SKILL"
    assert "interpret" in exc_info.value.detail["stale_skills"]


def test_stale_check_noop_when_clean(fake_home: Path) -> None:
    from codeprobe.cli.skills_cmd import check_stale_user_home_skills_or_raise

    # Must not raise.
    check_stale_user_home_skills_or_raise()
