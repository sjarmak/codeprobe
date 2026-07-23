"""Unit tests for scripts/check_release_artifacts.py.

Constructs synthetic wheel (zip) and sdist (tar.gz) fixtures in ``tmp_path``
instead of paying the real ``python -m build`` cost. Fixture shapes mirror
what ``python -m build`` actually produces for this project (verified
2026-07-22 by building the real artifacts): wheel entries have no "src/"
prefix; sdist entries are wrapped in a "<project>-<version>/" top-level
directory with a "src/" prefix underneath.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

_MODULE_PATH = (
    Path(__file__).resolve().parent.parent / "scripts" / "check_release_artifacts.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "check_release_artifacts", _MODULE_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
cra = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cra
_SPEC.loader.exec_module(cra)


PYPROJECT_TEMPLATE = """\
[project]
name = "codeprobe"
version = "{version}"
"""

CHANGELOG_TEMPLATE = """\
# Changelog

## {version} (2026-07-22)

- Something shipped.

## 0.10.1 (2026-05-08)

- Older entry.
"""


def _write_wheel(dist_dir: Path, version: str, skill_paths: list[str]) -> Path:
    wheel_path = dist_dir / f"codeprobe-{version}-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w") as zf:
        zf.writestr("codeprobe/__init__.py", "")
        for path in skill_paths:
            zf.writestr(path, "# skill\n")
        zf.writestr(
            f"codeprobe-{version}.dist-info/METADATA", "Metadata-Version: 2.1\n"
        )
    return wheel_path


def _write_sdist(dist_dir: Path, version: str, skill_paths: list[str]) -> Path:
    """``skill_paths`` are given WITHOUT the sdist's top-level
    "codeprobe-<version>/" wrapper directory — this helper adds it, matching
    the real sdist layout ``sdist_skill_paths`` strips back off."""
    sdist_path = dist_dir / f"codeprobe-{version}.tar.gz"
    top = f"codeprobe-{version}"
    with tarfile.open(sdist_path, "w:gz") as tf:
        for rel_path in [f"{top}/PKG-INFO", *(f"{top}/{p}" for p in skill_paths)]:
            data = b"# skill\n" if rel_path.endswith("SKILL.md") else b"metadata\n"
            info = tarfile.TarInfo(name=rel_path)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return sdist_path


EXPECTED_WHEEL_SKILLS: list[str] = sorted(cra.EXPECTED_WHEEL_SKILL_PATHS)
EXPECTED_SDIST_SKILLS: list[str] = sorted(cra.EXPECTED_SDIST_SKILL_PATHS)


@pytest.fixture
def dist_dir(tmp_path: Path) -> Path:
    d = tmp_path / "dist"
    d.mkdir()
    return d


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    return root


def _write_repo(repo_root: Path, version: str, changelog_version: str) -> None:
    (repo_root / "pyproject.toml").write_text(
        PYPROJECT_TEMPLATE.format(version=version)
    )
    (repo_root / "CHANGELOG.md").write_text(
        CHANGELOG_TEMPLATE.format(version=changelog_version)
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_check_release_artifacts_passes_on_clean_pair(
    dist_dir: Path, repo_root: Path
) -> None:
    _write_repo(repo_root, "0.12.0", "0.12.0")
    _write_wheel(dist_dir, "0.12.0", EXPECTED_WHEEL_SKILLS)
    _write_sdist(dist_dir, "0.12.0", EXPECTED_SDIST_SKILLS)

    result = cra.check_release_artifacts(dist_dir, repo_root)

    assert result.ok, result.violations


def test_check_release_artifacts_accepts_matching_tag_version(
    dist_dir: Path, repo_root: Path
) -> None:
    _write_repo(repo_root, "0.12.0", "0.12.0")
    _write_wheel(dist_dir, "0.12.0", EXPECTED_WHEEL_SKILLS)
    _write_sdist(dist_dir, "0.12.0", EXPECTED_SDIST_SKILLS)

    result = cra.check_release_artifacts(dist_dir, repo_root, tag_version="0.12.0")

    assert result.ok, result.violations


# ---------------------------------------------------------------------------
# (a) exactly one wheel + one sdist
# ---------------------------------------------------------------------------


def test_find_artifacts_rejects_missing_wheel(dist_dir: Path) -> None:
    (dist_dir / "codeprobe-0.12.0.tar.gz").write_bytes(b"")
    violations, wheel, sdist = cra.find_artifacts(dist_dir)
    assert wheel is None
    assert sdist is not None
    assert any("wheel" in v for v in violations)


def test_find_artifacts_rejects_missing_sdist(dist_dir: Path) -> None:
    (dist_dir / "codeprobe-0.12.0-py3-none-any.whl").write_bytes(b"")
    violations, wheel, sdist = cra.find_artifacts(dist_dir)
    assert sdist is None
    assert any("sdist" in v for v in violations)


def test_find_artifacts_rejects_multiple_wheels(dist_dir: Path) -> None:
    (dist_dir / "codeprobe-0.12.0-py3-none-any.whl").write_bytes(b"")
    (dist_dir / "codeprobe-0.11.0-py3-none-any.whl").write_bytes(b"")
    (dist_dir / "codeprobe-0.12.0.tar.gz").write_bytes(b"")
    violations, wheel, sdist = cra.find_artifacts(dist_dir)
    assert wheel is None
    assert any("found 2" in v for v in violations)


# ---------------------------------------------------------------------------
# (b)/(c) skill content — the 0.11.0 leak reproduction
# ---------------------------------------------------------------------------


def test_wheel_with_extra_unrelated_skill_rejected(
    dist_dir: Path, repo_root: Path
) -> None:
    _write_repo(repo_root, "0.12.0", "0.12.0")
    _write_wheel(dist_dir, "0.12.0", [*EXPECTED_WHEEL_SKILLS, "gc-mail/SKILL.md"])
    _write_sdist(dist_dir, "0.12.0", EXPECTED_SDIST_SKILLS)

    result = cra.check_release_artifacts(dist_dir, repo_root)

    assert not result.ok
    assert any("unexpected SKILL.md" in v and "gc-mail" in v for v in result.violations)


def test_sdist_containing_leaked_mine_tasks_skill_rejected(
    dist_dir: Path, repo_root: Path
) -> None:
    """Reproduces the actual 0.11.0 incident: a
    `recursive-include .claude/skills SKILL.md` rule swept the deprecated
    `.claude/skills/mine-tasks/SKILL.md` (pre-v0.6.0 name) into the sdist."""
    _write_repo(repo_root, "0.12.0", "0.12.0")
    _write_wheel(dist_dir, "0.12.0", EXPECTED_WHEEL_SKILLS)
    _write_sdist(
        dist_dir,
        "0.12.0",
        [*EXPECTED_SDIST_SKILLS, ".claude/skills/mine-tasks/SKILL.md"],
    )

    result = cra.check_release_artifacts(dist_dir, repo_root)

    assert not result.ok
    assert any("0.11.0" in v and "mine-tasks" in v for v in result.violations)


def test_wheel_missing_an_expected_skill_rejected(
    dist_dir: Path, repo_root: Path
) -> None:
    _write_repo(repo_root, "0.12.0", "0.12.0")
    _write_wheel(dist_dir, "0.12.0", EXPECTED_WHEEL_SKILLS[:-1])
    _write_sdist(dist_dir, "0.12.0", EXPECTED_SDIST_SKILLS)

    result = cra.check_release_artifacts(dist_dir, repo_root)

    assert not result.ok
    assert any("missing expected skill" in v for v in result.violations)


# ---------------------------------------------------------------------------
# (d) version drift
# ---------------------------------------------------------------------------


def test_version_mismatch_between_wheel_and_pyproject_rejected(
    dist_dir: Path, repo_root: Path
) -> None:
    _write_repo(repo_root, "0.12.0", "0.12.0")
    _write_wheel(dist_dir, "0.11.9", EXPECTED_WHEEL_SKILLS)  # stale wheel
    _write_sdist(dist_dir, "0.12.0", EXPECTED_SDIST_SKILLS)

    result = cra.check_release_artifacts(dist_dir, repo_root)

    assert not result.ok
    assert any("wheel filename version" in v for v in result.violations)


def test_tag_version_mismatch_against_pyproject_rejected(
    dist_dir: Path, repo_root: Path
) -> None:
    _write_repo(repo_root, "0.12.0", "0.12.0")
    _write_wheel(dist_dir, "0.12.0", EXPECTED_WHEEL_SKILLS)
    _write_sdist(dist_dir, "0.12.0", EXPECTED_SDIST_SKILLS)

    result = cra.check_release_artifacts(dist_dir, repo_root, tag_version="0.11.9")

    assert not result.ok
    assert any("from the pushed tag" in v for v in result.violations)


def test_artifact_version_parses_wheel_and_sdist_filenames() -> None:
    assert (
        cra.artifact_version(Path("codeprobe-0.12.0-py3-none-any.whl")) == "0.12.0"
    )
    assert cra.artifact_version(Path("codeprobe-0.12.0.tar.gz")) == "0.12.0"
    assert cra.artifact_version(Path("not-codeprobe-0.12.0.whl")) is None


# ---------------------------------------------------------------------------
# (e) CHANGELOG heading
# ---------------------------------------------------------------------------


def test_missing_changelog_heading_rejected(dist_dir: Path, repo_root: Path) -> None:
    _write_repo(repo_root, "0.12.0", "0.11.0")  # heading only for the old version
    _write_wheel(dist_dir, "0.12.0", EXPECTED_WHEEL_SKILLS)
    _write_sdist(dist_dir, "0.12.0", EXPECTED_SDIST_SKILLS)

    result = cra.check_release_artifacts(dist_dir, repo_root)

    assert not result.ok
    assert any("no '## 0.12.0' heading" in v for v in result.violations)


def test_changelog_heading_does_not_match_dotted_prefix(
    dist_dir: Path, repo_root: Path
) -> None:
    """A "0.11" heading must NOT satisfy a check for "0.11.0" — \\b alone
    would wrongly accept this since the digit->dot transition is itself a
    word boundary."""
    (repo_root / "pyproject.toml").write_text(
        PYPROJECT_TEMPLATE.format(version="0.11.0")
    )
    (repo_root / "CHANGELOG.md").write_text("# Changelog\n\n## 0.11 (2026-05-11)\n")
    _write_wheel(dist_dir, "0.11.0", EXPECTED_WHEEL_SKILLS)
    _write_sdist(dist_dir, "0.11.0", EXPECTED_SDIST_SKILLS)

    result = cra.check_release_artifacts(dist_dir, repo_root)

    assert not result.ok
    assert any("no '## 0.11.0' heading" in v for v in result.violations)


def test_missing_changelog_file_rejected(dist_dir: Path, repo_root: Path) -> None:
    (repo_root / "pyproject.toml").write_text(
        PYPROJECT_TEMPLATE.format(version="0.12.0")
    )
    _write_wheel(dist_dir, "0.12.0", EXPECTED_WHEEL_SKILLS)
    _write_sdist(dist_dir, "0.12.0", EXPECTED_SDIST_SKILLS)

    result = cra.check_release_artifacts(dist_dir, repo_root)

    assert not result.ok
    assert any("does not exist" in v for v in result.violations)


# ---------------------------------------------------------------------------
# Archive listing helpers
# ---------------------------------------------------------------------------


def test_wheel_skill_paths_extracts_only_skill_md(tmp_path: Path) -> None:
    wheel_path = _write_wheel(tmp_path, "0.12.0", EXPECTED_WHEEL_SKILLS)
    assert sorted(cra.wheel_skill_paths(wheel_path)) == EXPECTED_WHEEL_SKILLS


def test_sdist_skill_paths_strips_top_level_dir(tmp_path: Path) -> None:
    sdist_path = _write_sdist(tmp_path, "0.12.0", EXPECTED_SDIST_SKILLS)
    assert sorted(cra.sdist_skill_paths(sdist_path)) == EXPECTED_SDIST_SKILLS


# ---------------------------------------------------------------------------
# CLI (main())
# ---------------------------------------------------------------------------


def test_main_exits_zero_on_clean_pair(
    dist_dir: Path, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_repo(repo_root, "0.12.0", "0.12.0")
    _write_wheel(dist_dir, "0.12.0", EXPECTED_WHEEL_SKILLS)
    _write_sdist(dist_dir, "0.12.0", EXPECTED_SDIST_SKILLS)

    exit_code = cra.main([str(dist_dir), "--repo", str(repo_root)])

    assert exit_code == 0
    assert "OK:" in capsys.readouterr().out


def test_main_exits_one_and_reports_violations(
    dist_dir: Path, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_repo(repo_root, "0.12.0", "0.11.0")
    _write_wheel(dist_dir, "0.12.0", EXPECTED_WHEEL_SKILLS)
    _write_sdist(dist_dir, "0.12.0", EXPECTED_SDIST_SKILLS)

    exit_code = cra.main([str(dist_dir), "--repo", str(repo_root)])

    assert exit_code == 1
    assert "RELEASE ARTIFACT CHECK FAILED" in capsys.readouterr().err


def test_main_exits_two_on_missing_dist_dir(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = cra.main([str(tmp_path / "nope")])
    assert exit_code == 2
    assert "not a directory" in capsys.readouterr().err
