"""Tests for ``codeprobe skills install`` (bead codeprobe-f7rl.41).

The command copies the packaged agent skills (``codeprobe.skills_data``
wheel package data) into a ``.claude/skills`` directory. These tests
exercise the fresh-install, idempotency, conflict-refusal, ``--force``,
``--user``, and flag-mutex paths against ``tmp_path`` destinations so no
real skill tree is ever touched.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from codeprobe.cli.skills_cmd import _packaged_skills, skills

EXPECTED_SKILLS = {
    "codeprobe-calibrate",
    "codeprobe-check-infra",
    "codeprobe-interpret",
    "codeprobe-mine",
    "codeprobe-run",
}


def _install(args: list[str], env: dict[str, str] | None = None):
    runner = CliRunner()
    return runner.invoke(skills, ["install", *args, "--json"], env=env)


def _payload(result) -> dict:
    return json.loads(result.output.splitlines()[-1])


def test_packaged_skills_enumerates_exactly_five() -> None:
    packaged = _packaged_skills()
    assert {name for name, _ in packaged} == EXPECTED_SKILLS
    assert all(content.startswith("---\n") for _, content in packaged)


def test_install_fresh_dest_materializes_five_skills(tmp_path: Path) -> None:
    dest = tmp_path / "skills"
    result = _install(["--dest", str(dest)])
    assert result.exit_code == 0, result.output

    payload = _payload(result)
    assert payload["ok"] is True
    data = payload["data"]
    assert data["dest"] == str(dest)
    assert sorted(data["installed"]) == sorted(EXPECTED_SKILLS)
    assert data["unchanged"] == []
    assert data["conflicts_overwritten"] == []
    assert data["skill_count"] == 5

    on_disk = {p.parent.name for p in dest.glob("*/SKILL.md")}
    assert on_disk == EXPECTED_SKILLS


def test_install_second_run_is_unchanged_and_writes_nothing(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "skills"
    assert _install(["--dest", str(dest)]).exit_code == 0
    mtimes = {
        p.parent.name: p.stat().st_mtime_ns for p in dest.glob("*/SKILL.md")
    }

    result = _install(["--dest", str(dest)])
    assert result.exit_code == 0, result.output
    data = _payload(result)["data"]
    assert data["installed"] == []
    assert sorted(data["unchanged"]) == sorted(EXPECTED_SKILLS)
    assert data["skill_count"] == 5

    after = {
        p.parent.name: p.stat().st_mtime_ns for p in dest.glob("*/SKILL.md")
    }
    assert after == mtimes, "unchanged skills must not be rewritten"


def test_install_conflict_refuses_and_leaves_file_untouched(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "skills"
    assert _install(["--dest", str(dest)]).exit_code == 0
    edited = dest / "codeprobe-mine" / "SKILL.md"
    edited.write_text("# locally edited\n", encoding="utf-8")

    result = _install(["--dest", str(dest)])
    assert result.exit_code != 0, result.output
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "SKILL_INSTALL_CONFLICT"
    assert payload["error"]["detail"]["conflicts"] == ["codeprobe-mine"]

    assert edited.read_text(encoding="utf-8") == "# locally edited\n", (
        "conflict refusal must not write anything"
    )


def test_install_force_overwrites_conflicts(tmp_path: Path) -> None:
    dest = tmp_path / "skills"
    assert _install(["--dest", str(dest)]).exit_code == 0
    edited = dest / "codeprobe-run" / "SKILL.md"
    edited.write_text("# locally edited\n", encoding="utf-8")

    result = _install(["--dest", str(dest), "--force"])
    assert result.exit_code == 0, result.output
    data = _payload(result)["data"]
    assert data["conflicts_overwritten"] == ["codeprobe-run"]
    assert len(data["unchanged"]) == 4
    assert data["skill_count"] == 5

    packaged = dict(_packaged_skills())
    assert edited.read_text(encoding="utf-8") == packaged["codeprobe-run"]


def test_install_user_flag_targets_home_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    result = _install(["--user"], env={"HOME": str(tmp_path)})
    assert result.exit_code == 0, result.output

    dest = tmp_path / ".claude" / "skills"
    assert _payload(result)["data"]["dest"] == str(dest)
    on_disk = {p.parent.name for p in dest.glob("*/SKILL.md")}
    assert on_disk == EXPECTED_SKILLS


@pytest.mark.parametrize(
    "flags",
    [
        ["--dest", "DEST", "--user"],
        ["--dest", "DEST", "--project"],
        ["--user", "--project"],
    ],
)
def test_install_destination_selectors_are_mutually_exclusive(
    tmp_path: Path, flags: list[str]
) -> None:
    args = [str(tmp_path) if f == "DEST" else f for f in flags]
    result = _install(args)
    assert result.exit_code != 0
    payload = _payload(result)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "MUTEX_FLAGS"


def test_install_defaults_to_user_home_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The repo under test is an argument, not the install directory.

    Scoping the skills to whatever directory the customer happened to run
    ``pip install`` from would make the common case — point codeprobe at
    some other checkout — silently skill-less.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(
            skills, ["install", "--json"], env={"HOME": str(tmp_path)}
        )
        assert result.exit_code == 0, result.output
        dest = tmp_path / ".claude" / "skills"
        assert _payload(result)["data"]["dest"] == str(dest)
        on_disk = {p.parent.name for p in dest.glob("*/SKILL.md")}
        assert on_disk == EXPECTED_SKILLS


def test_install_project_flag_targets_cwd_claude_skills(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
        result = runner.invoke(skills, ["install", "--project", "--json"])
        assert result.exit_code == 0, result.output
        dest = Path(cwd) / ".claude" / "skills"
        assert _payload(result)["data"]["dest"] == str(dest)
        on_disk = {p.parent.name for p in dest.glob("*/SKILL.md")}
        assert on_disk == EXPECTED_SKILLS
