"""Tests for ``scripts/pre_tag_check.py``.

Stub verdict files and a stub repo (pyproject + CHANGELOG) in tmp_path — no
real acceptance-loop runs, no real git tags. ``tag_exists`` is monkeypatched
except in the one test that exercises it against a real ``git init`` repo.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "pre_tag_check.py"
_SPEC = importlib.util.spec_from_file_location("pre_tag_check_script", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
pre_tag_check = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = pre_tag_check
_SPEC.loader.exec_module(pre_tag_check)

VERSION = "0.13.0"

# Bound before the autouse no_existing_tag fixture patches the module attr.
_real_tag_exists = pre_tag_check.tag_exists


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_verdict(
    history: Path,
    index: int,
    *,
    status: str = "EVALUATED",
    all_pass: bool = True,
    eval_mode: str | None = "full",
    no_handler_criteria: list[dict[str, str]] | None = None,
) -> Path:
    history.mkdir(parents=True, exist_ok=True)
    path = history / f"verdict-{index:04d}.json"
    path.write_text(
        json.dumps(
            {
                "iteration": index,
                "status": status,
                "all_pass": all_pass,
                "eval_mode": eval_mode,
                "pass_count": 5,
                "fail_count": 0 if all_pass else 1,
                "failures": [],
                "no_handler_criteria": no_handler_criteria or [],
            }
        )
    )
    return path


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A stub repo root with pyproject, changelog, and green full-mode history."""
    (tmp_path / "pyproject.toml").write_text(
        textwrap.dedent(f"""
            [project]
            name = "codeprobe"
            version = "{VERSION}"
            """).strip()
    )
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## {VERSION} (2026-07-23)\n\n- things\n"
    )
    history = tmp_path / "acceptance" / "verdict-history"
    _write_verdict(history, 1)
    _write_verdict(history, 2)
    return tmp_path


@pytest.fixture(autouse=True)
def no_existing_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pre_tag_check, "tag_exists", lambda repo_root, tag: False)


def _run(repo_root: Path) -> int:
    return pre_tag_check.main(["--repo-root", str(repo_root)])


# ---------------------------------------------------------------------------
# Ready path
# ---------------------------------------------------------------------------


def test_ready_when_all_preconditions_hold(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(repo) == 0
    assert f"READY to tag v{VERSION}" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Verdict-history failures
# ---------------------------------------------------------------------------


def test_fails_with_fewer_than_two_verdicts(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    history = repo / "acceptance" / "verdict-history"
    (history / "verdict-0002.json").unlink()
    assert _run(repo) == 1
    err = capsys.readouterr().err
    assert "acceptance_loop.py" in err


def test_fails_with_no_history_dir(
    tmp_path: Path, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import shutil

    shutil.rmtree(repo / "acceptance")
    assert _run(repo) == 1
    assert "acceptance_loop.py" in capsys.readouterr().err


def test_fails_when_newest_verdict_incomplete(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    history = repo / "acceptance" / "verdict-history"
    _write_verdict(history, 2, status="INCOMPLETE", all_pass=False)
    assert _run(repo) == 1
    assert "EVALUATED" in capsys.readouterr().err


def test_fails_when_verdict_not_all_pass(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    history = repo / "acceptance" / "verdict-history"
    _write_verdict(history, 2, all_pass=False)
    assert _run(repo) == 1


def test_uses_newest_two_ignoring_older_failures(repo: Path) -> None:
    """An old red verdict must not block once two newer greens exist."""
    history = repo / "acceptance" / "verdict-history"
    _write_verdict(history, 1, status="INCOMPLETE", all_pass=False, eval_mode=None)
    _write_verdict(history, 3, eval_mode="full")
    assert _run(repo) == 0


# ---------------------------------------------------------------------------
# eval_mode=full requirement
# ---------------------------------------------------------------------------


def test_fails_when_no_full_mode_verdict(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    history = repo / "acceptance" / "verdict-history"
    _write_verdict(history, 1, eval_mode=None)
    _write_verdict(history, 2, eval_mode=None)
    assert _run(repo) == 1
    err = capsys.readouterr().err
    assert "--eval-mode full" in err
    assert "NOT release evidence" in err


def test_legacy_verdict_without_eval_mode_key_counts_as_not_full(
    repo: Path,
) -> None:
    history = repo / "acceptance" / "verdict-history"
    for index in (1, 2):
        path = history / f"verdict-{index:04d}.json"
        data = json.loads(path.read_text())
        del data["eval_mode"]
        path.write_text(json.dumps(data))
    assert _run(repo) == 1


def test_one_full_mode_verdict_suffices(repo: Path) -> None:
    history = repo / "acceptance" / "verdict-history"
    _write_verdict(history, 1, eval_mode=None)
    _write_verdict(history, 2, eval_mode="full")
    assert _run(repo) == 0


# ---------------------------------------------------------------------------
# Handler-less critical/high criteria
# ---------------------------------------------------------------------------


def test_fails_when_critical_criterion_is_handler_less(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A critical criterion with no registered Verifier handler must never
    be silently READY — it was never evaluated in ANY eval mode."""
    history = repo / "acceptance" / "verdict-history"
    _write_verdict(
        history,
        2,
        no_handler_criteria=[
            {"criterion_id": "LOG-STDERR-003", "tier": "behavioral", "severity": "critical"}
        ],
    )
    assert _run(repo) == 1
    err = capsys.readouterr().err
    assert "LOG-STDERR-003" in err
    assert "no registered Verifier handler" in err


def test_warns_but_does_not_fail_on_medium_handler_less_criterion(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    history = repo / "acceptance" / "verdict-history"
    _write_verdict(
        history,
        2,
        no_handler_criteria=[
            {"criterion_id": "SOME-LOW-PRIORITY", "tier": "behavioral", "severity": "medium"}
        ],
    )
    assert _run(repo) == 0
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "SOME-LOW-PRIORITY" in out


def test_passes_when_no_handler_criteria_field_absent(repo: Path) -> None:
    """Verdicts written before this field existed must not break the gate."""
    history = repo / "acceptance" / "verdict-history"
    for index in (1, 2):
        path = history / f"verdict-{index:04d}.json"
        data = json.loads(path.read_text())
        del data["no_handler_criteria"]
        path.write_text(json.dumps(data))
    assert _run(repo) == 0


# ---------------------------------------------------------------------------
# Changelog and tag preconditions
# ---------------------------------------------------------------------------


def test_fails_without_changelog_heading(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / "CHANGELOG.md").write_text("# Changelog\n\n## 0.12.0\n")
    assert _run(repo) == 1
    assert f"## {VERSION}" in capsys.readouterr().err


def test_fails_when_tag_already_exists(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(pre_tag_check, "tag_exists", lambda repo_root, tag: True)
    assert _run(repo) == 1
    assert "bump" in capsys.readouterr().err


def test_tag_exists_against_real_git_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "--allow-empty", "-q", "-m", "x"],
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin",
        },
    )
    assert _real_tag_exists(tmp_path, "v9.9.9") is False
    subprocess.run(["git", "-C", str(tmp_path), "tag", "v9.9.9"], check=True)
    assert _real_tag_exists(tmp_path, "v9.9.9") is True


# ---------------------------------------------------------------------------
# Unit-level helpers
# ---------------------------------------------------------------------------


def test_find_newest_verdicts_orders_oldest_to_newest(tmp_path: Path) -> None:
    for index in (3, 1, 2):
        _write_verdict(tmp_path, index)
    newest = pre_tag_check.find_newest_verdicts(tmp_path)
    assert [p.name for p in newest] == ["verdict-0002.json", "verdict-0003.json"]


def test_changelog_heading_requires_word_boundary(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## 0.13.01\n")
    assert pre_tag_check.changelog_has_heading(changelog, "0.13.0") is False
    changelog.write_text("## 0.13.0 (2026-07-23)\n")
    assert pre_tag_check.changelog_has_heading(changelog, "0.13.0") is True
