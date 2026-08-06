"""Tests for the install banner, next-step CTA, and version stamp (codeprobe-ybw7).

``codeprobe skills install`` is the moment a pip customer hands codeprobe
to their coding agent, so it renders a banner, tells the operator what to
ask for next, and stamps the destination with the installing version.
The stamp is what lets ``codeprobe doctor`` notice that a later
``pip install -U codeprobe`` left inert SKILL.md copies behind.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe import __version__
from codeprobe.cli._banner import render_banner, should_print_banner
from codeprobe.cli.doctor_cmd import _check_installed_skill_version
from codeprobe.cli.skills_cmd import (
    _VERSION_STAMP,
    _version_tuple,
    inspect_installed_skills,
    installed_skill_drift,
    read_version_stamp,
    skills,
)


def _install(args: list[str]):
    return CliRunner().invoke(skills, ["install", *args])


def _stamp(dest: Path) -> Path:
    return dest / _VERSION_STAMP


# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------


def test_render_banner_plain_has_wordmark_and_version() -> None:
    plain = render_banner("9.9.9", color=False)
    assert "█▀▀ █▀█ █▀▄" in plain, "CODEPROBE wordmark missing"
    assert "9.9.9" in plain
    assert "\x1b[" not in plain, "color=False must emit no ANSI sequences"


def test_render_banner_color_wraps_in_ansi() -> None:
    assert "\x1b[" in render_banner("9.9.9", color=True)


@pytest.mark.parametrize(
    ("mode", "use_rich", "expected"),
    [
        ("pretty", True, True),
        ("pretty", False, False),
        ("json", True, False),
        ("json-lines", True, False),
    ],
)
def test_banner_only_on_pretty_tty(mode: str, use_rich: bool, expected: bool) -> None:
    assert should_print_banner(mode=mode, use_rich=use_rich) is expected


def test_json_install_emits_no_banner(tmp_path: Path) -> None:
    result = _install(["--dest", str(tmp_path / "skills"), "--json"])
    assert result.exit_code == 0, result.output
    assert "█" not in result.output
    assert json.loads(result.output.splitlines()[-1])["ok"] is True


# ---------------------------------------------------------------------------
# Next-step CTA
# ---------------------------------------------------------------------------


def test_pretty_install_prints_next_steps(tmp_path: Path) -> None:
    dest = tmp_path / ".claude" / "skills"
    result = _install(["--dest", str(dest), "--no-json"])
    assert result.exit_code == 0, result.output
    assert "Done — 5 skill(s)" in result.output
    assert "mine 5 eval tasks from this repo" in result.output
    assert "model-invoked, not slash commands" in result.output
    assert "~/src/myproject" in result.output, (
        "the repo to mine is an argument — never imply it must be the cwd"
    )
    assert "type /" not in result.output, (
        "skills are user-invocable: false — never advertise a slash command"
    )


def test_default_install_says_skills_are_machine_wide(
    tmp_path: Path, monkeypatch
) -> None:
    """The default lands in ~/.claude/skills — say so, don't say "this project"."""
    monkeypatch.setenv("HOME", str(tmp_path))
    result = CliRunner().invoke(
        skills, ["install", "--no-json"], env={"HOME": str(tmp_path)}
    )
    assert result.exit_code == 0, result.output
    assert "Available in every project on this machine" in result.output
    assert str(tmp_path / ".claude" / "skills") in result.output


def test_project_install_scopes_the_message_and_points_at_user(
    tmp_path: Path,
) -> None:
    result = _install(["--dest", str(tmp_path / ".claude" / "skills"), "--no-json"])
    assert result.exit_code == 0, result.output
    assert "in this project picks these up" in result.output
    assert "Use --user to make them available everywhere" in result.output


def test_non_claude_dest_warns_skills_will_not_load(tmp_path: Path) -> None:
    result = _install(["--dest", str(tmp_path / "elsewhere"), "--no-json"])
    assert result.exit_code == 0, result.output
    assert "only loads skills from ./.claude/skills" in result.output


# ---------------------------------------------------------------------------
# Version stamp
# ---------------------------------------------------------------------------


def test_install_writes_version_stamp(tmp_path: Path) -> None:
    dest = tmp_path / "skills"
    result = _install(["--dest", str(dest), "--json"])
    assert result.exit_code == 0, result.output

    assert _stamp(dest).read_text(encoding="utf-8") == f"{__version__}\n"
    data = json.loads(result.output.splitlines()[-1])["data"]
    assert data["version"] == __version__
    assert data["stamp_path"] == str(_stamp(dest))


def test_stamp_refreshed_on_all_unchanged_rerun(tmp_path: Path) -> None:
    dest = tmp_path / "skills"
    assert _install(["--dest", str(dest), "--json"]).exit_code == 0
    _stamp(dest).write_text("0.0.1\n", encoding="utf-8")

    result = _install(["--dest", str(dest), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output.splitlines()[-1])["data"]
    assert data["unchanged"], "precondition: this run copies nothing"
    assert read_version_stamp(dest) == __version__, (
        "an upgraded package must clear its own drift on re-run"
    )


def test_conflict_refusal_leaves_stamp_untouched(tmp_path: Path) -> None:
    dest = tmp_path / "skills"
    assert _install(["--dest", str(dest), "--json"]).exit_code == 0
    _stamp(dest).write_text("0.0.1\n", encoding="utf-8")
    (dest / "codeprobe-mine" / "SKILL.md").write_text("# edited\n", encoding="utf-8")

    result = _install(["--dest", str(dest), "--json"])
    assert result.exit_code != 0
    assert read_version_stamp(dest) == "0.0.1", (
        "a refused install must not claim the skills were refreshed"
    )


def test_read_version_stamp_absent_and_blank(tmp_path: Path) -> None:
    assert read_version_stamp(tmp_path) is None
    _stamp(tmp_path).write_text("  \n", encoding="utf-8")
    assert read_version_stamp(tmp_path) is None


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.14.0", (0, 14, 0)),
        ("0.14.0rc2", (0, 14, 0)),
        ("1.2.3.4", (1, 2, 3)),
        ("garbage", ()),
    ],
)
def test_version_tuple_drops_prerelease_suffixes(
    raw: str, expected: tuple[int, ...]
) -> None:
    assert _version_tuple(raw) == expected


def _install_into(dest: Path) -> None:
    assert _install(["--dest", str(dest), "--json"]).exit_code == 0


def test_untouched_directory_is_not_drift(tmp_path: Path) -> None:
    """A ~/.claude/skills full of other people's skills is not our business."""
    (tmp_path / "someone-elses-skill").mkdir()
    assert inspect_installed_skills(tmp_path, "0.14.0") is None


def test_clean_install_reports_no_drift(tmp_path: Path) -> None:
    _install_into(tmp_path)
    report = inspect_installed_skills(tmp_path, __version__)
    assert report is not None
    assert report.drifted is False
    assert (report.stale, report.missing, report.version_direction) == ((), (), None)


def test_version_behind_when_stamp_is_older(tmp_path: Path) -> None:
    _install_into(tmp_path)
    _stamp(tmp_path).write_text("0.13.0\n", encoding="utf-8")
    report = inspect_installed_skills(tmp_path, "0.14.0")
    assert report is not None and report.version_direction == "behind"
    assert report.drifted is True


def test_version_ahead_when_stamp_is_newer(tmp_path: Path) -> None:
    _install_into(tmp_path)
    _stamp(tmp_path).write_text("0.15.0\n", encoding="utf-8")
    report = inspect_installed_skills(tmp_path, "0.14.0")
    assert report is not None and report.version_direction == "ahead"


def test_prerelease_difference_counts_as_behind(tmp_path: Path) -> None:
    """Equal release tuples, unequal text — never advise a downgrade path."""
    _install_into(tmp_path)
    _stamp(tmp_path).write_text("0.14.0rc1\n", encoding="utf-8")
    report = inspect_installed_skills(tmp_path, "0.14.0rc2")
    assert report is not None and report.version_direction == "behind"


def test_edited_skill_is_content_drift_at_a_matching_version(
    tmp_path: Path,
) -> None:
    """The gap a version stamp alone cannot see.

    An editable checkout that moved ahead of an installed copy, or a hand
    edit, leaves the stamp it was written with — so only comparing bytes
    catches it.
    """
    _install_into(tmp_path)
    (tmp_path / "codeprobe-mine" / "SKILL.md").write_text("# edited\n", "utf-8")

    report = inspect_installed_skills(tmp_path, __version__)
    assert report is not None
    assert report.version_direction is None, "the stamp still matches"
    assert report.stale == ("codeprobe-mine",)
    assert report.drifted is True


def test_partially_installed_tree_reports_missing(tmp_path: Path) -> None:
    _install_into(tmp_path)
    shutil.rmtree(tmp_path / "codeprobe-run")

    report = inspect_installed_skills(tmp_path, __version__)
    assert report is not None
    assert report.missing == ("codeprobe-run",)
    assert report.stale == ()


def test_installed_skill_drift_scans_supplied_roots(tmp_path: Path) -> None:
    clean, stale = tmp_path / "a", tmp_path / "b"
    _install_into(clean)
    _install_into(stale)
    _stamp(stale).write_text("0.0.1\n", encoding="utf-8")

    drift = installed_skill_drift([clean, stale], __version__)
    assert [d.dest for d in drift] == [stale]


def test_installed_skill_drift_deduplicates_roots(tmp_path: Path) -> None:
    """cwd and $HOME can be the same directory — report it once, not twice."""
    _install_into(tmp_path)
    _stamp(tmp_path).write_text("0.0.1\n", encoding="utf-8")
    assert len(installed_skill_drift([tmp_path, tmp_path], __version__)) == 1


# ---------------------------------------------------------------------------
# Doctor wiring
# ---------------------------------------------------------------------------


def test_doctor_passes_when_no_stamp(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    result = _check_installed_skill_version()
    assert result.passed is True
    assert result.warn_only is True


def test_doctor_check_name_does_not_look_like_a_tool_check(
    tmp_path: Path, monkeypatch
) -> None:
    """doctor's tool checks are selected by a ``CLI`` name suffix.

    ``tests/test_doctor_checks.py::test_tool_not_found`` asserts every
    ``*CLI`` check fails when ``shutil.which`` finds nothing, so a check
    named "... CLI" that passes for unrelated reasons breaks it.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert not _check_installed_skill_version().name.endswith("CLI")


def _home_install(tmp_path: Path, monkeypatch) -> Path:
    """Point cwd and $HOME at tmp_path, install, and return the skills dir."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    dest = tmp_path / ".claude" / "skills"
    _install_into(dest)
    return dest


def test_doctor_passes_on_a_clean_install(tmp_path: Path, monkeypatch) -> None:
    _home_install(tmp_path, monkeypatch)
    result = _check_installed_skill_version()
    assert result.passed is True


def test_doctor_warns_and_advises_install_when_behind(
    tmp_path: Path, monkeypatch
) -> None:
    dest = _home_install(tmp_path, monkeypatch)
    _stamp(dest).write_text("0.0.1\n", encoding="utf-8")

    result = _check_installed_skill_version()
    assert result.passed is False
    assert result.warn_only is True, "stale skills must not fail doctor outright"
    assert "0.0.1" in result.detail
    assert "codeprobe skills install" in result.fix


def test_doctor_advises_package_upgrade_when_skills_are_newer(
    tmp_path: Path, monkeypatch
) -> None:
    dest = _home_install(tmp_path, monkeypatch)
    _stamp(dest).write_text("99.0.0\n", encoding="utf-8")

    result = _check_installed_skill_version()
    assert result.passed is False
    assert "pip install -U codeprobe" in result.fix
    assert "would downgrade" in result.fix


def test_doctor_reports_content_drift_at_a_matching_version(
    tmp_path: Path, monkeypatch
) -> None:
    dest = _home_install(tmp_path, monkeypatch)
    (dest / "codeprobe-run" / "SKILL.md").write_text("# edited\n", encoding="utf-8")

    result = _check_installed_skill_version()
    assert result.passed is False
    assert "differ from the packaged copy" in result.detail
    assert "codeprobe-run" in result.detail
    assert "--force" in result.fix, "the refusal path needs naming in the fix"


def test_doctor_reports_a_missing_skill(tmp_path: Path, monkeypatch) -> None:
    dest = _home_install(tmp_path, monkeypatch)
    shutil.rmtree(dest / "codeprobe-calibrate")

    result = _check_installed_skill_version()
    assert result.passed is False
    assert "not installed (codeprobe-calibrate)" in result.detail
