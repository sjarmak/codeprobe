"""Interactive/non-interactive gating tests for discover_related_repos.

Verifies:
- Non-interactive mode without --cross-repo flag raises RuntimeError
- Non-interactive mode with flag returns candidates silently
- Interactive mode prompts and honors y/n replies
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.mining import multi_repo
from codeprobe.mining.multi_repo import discover_related_repos


def _make_repo(tmp_path: Path) -> Path:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "x", "dependencies": {"alpha": "1", "beta": "1"}}),
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.js").write_text(
        "import alpha from 'alpha';\n"
        "import beta from 'beta';\n",
        encoding="utf-8",
    )
    return tmp_path


def test_non_interactive_without_flag_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_repo(tmp_path)
    # Force non-TTY stdin
    monkeypatch.setattr(multi_repo, "_stdin_isatty", lambda: False)
    with pytest.raises(RuntimeError, match="cross_repo_confirmed"):
        discover_related_repos(tmp_path, hints=None)


def test_non_interactive_autodetected_without_flag_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When interactive=None and stdin is not a TTY, the gate must activate."""
    _make_repo(tmp_path)
    monkeypatch.setattr(multi_repo, "_stdin_isatty", lambda: False)
    with pytest.raises(RuntimeError):
        discover_related_repos(tmp_path, hints=None, interactive=None)


def test_non_interactive_with_flag_returns_candidates(tmp_path: Path) -> None:
    _make_repo(tmp_path)
    ranked = discover_related_repos(
        tmp_path, hints=None, interactive=False, cross_repo_confirmed=True
    )
    hints = {c.hint for c in ranked}
    assert {"alpha", "beta"} <= hints


def test_interactive_accepts_all(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_repo(tmp_path)
    # Force TTY and feed "y" for every prompt.
    monkeypatch.setattr(multi_repo, "_stdin_isatty", lambda: True)
    replies = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))

    ranked = discover_related_repos(tmp_path, hints=None)
    hints = {c.hint for c in ranked}
    assert hints == {"alpha", "beta"}


def test_interactive_rejects_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_repo(tmp_path)
    monkeypatch.setattr(multi_repo, "_stdin_isatty", lambda: True)
    # ranking is alpha first, beta second (equal hits → alphabetical)
    # Reject alpha, accept beta.
    replies = iter(["n", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))

    ranked = discover_related_repos(tmp_path, hints=None)
    hints = [c.hint for c in ranked]
    assert hints == ["beta"]


def test_interactive_default_is_reject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Blank/non-y reply must be treated as rejection."""
    _make_repo(tmp_path)
    monkeypatch.setattr(multi_repo, "_stdin_isatty", lambda: True)
    replies = iter(["", "no"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))

    ranked = discover_related_repos(tmp_path, hints=None)
    assert ranked == []


def test_explicit_interactive_true_prompts_even_on_non_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """interactive=True overrides autodetect — prompt appears even without TTY."""
    _make_repo(tmp_path)
    monkeypatch.setattr(multi_repo, "_stdin_isatty", lambda: False)
    replies = iter(["y", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(replies))

    ranked = discover_related_repos(tmp_path, hints=None, interactive=True)
    assert {c.hint for c in ranked} == {"alpha", "beta"}


def test_explicit_interactive_false_without_flag_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """interactive=False without cross_repo_confirmed raises even on a TTY."""
    _make_repo(tmp_path)
    monkeypatch.setattr(multi_repo, "_stdin_isatty", lambda: True)
    with pytest.raises(RuntimeError):
        discover_related_repos(tmp_path, hints=None, interactive=False)
