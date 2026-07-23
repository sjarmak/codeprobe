"""Host-aware narrative-source resolution (codeprobe-f7rl.22).

PR/MR narrative fetch is gh-CLI-only, so ``_resolve_narrative_source`` must
refuse honestly on non-GitHub hosts (naming the GitHub-only limitation and
the ``--narrative-source commits`` fallback) instead of probing gh and then
mis-diagnosing the repo as "squash-only or no-remote history". GitHub and
local hosts keep the existing behavior, and explicit selections bypass the
host check entirely.

Both ``detect_source`` and ``has_pr_narratives`` are imported inside
``_resolve_narrative_source`` from ``codeprobe.mining.sources``, so
monkeypatching that module patches the CLI's view too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from codeprobe.cli.errors import PrescriptiveError
from codeprobe.cli.mine_cmd import _resolve_narrative_source
from codeprobe.mining.sources import RepoSource


def _patch_host(monkeypatch: pytest.MonkeyPatch, host: str) -> None:
    monkeypatch.setattr(
        "codeprobe.mining.sources.detect_source",
        lambda path: RepoSource(
            host=host,
            owner="acme",
            repo="widgets",
            remote_url=f"git@{host}.example.com:acme/widgets.git",
        ),
    )


def _forbid_gh_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(path: Path, timeout: int = 10) -> bool:
        raise AssertionError(
            "has_pr_narratives must not be called (no gh subprocess "
            "may be spawned on this path)"
        )

    monkeypatch.setattr("codeprobe.mining.sources.has_pr_narratives", _fail)


def test_gitlab_host_raises_accurate_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitLab remotes get the GitHub-only message, not 'squash-only'."""
    _patch_host(monkeypatch, "gitlab")
    _forbid_gh_probe(monkeypatch)

    with pytest.raises(PrescriptiveError) as excinfo:
        _resolve_narrative_source(
            (), tmp_path, tasks_mined=True, pr_bodies={}
        )

    exc = excinfo.value
    assert exc.code == "NARRATIVE_SOURCE_UNDETECTABLE"
    assert "GitHub-only" in exc.message
    assert "gitlab" in exc.message
    assert "squash-only" not in exc.message
    assert exc.next_try_flag == "--narrative-source"
    assert exc.next_try_value == "commits"
    assert exc.detail == {"host": "gitlab"}


@pytest.mark.parametrize("host", ["bitbucket", "azure", "gitea", "self-hosted"])
def test_other_non_github_hosts_also_refuse(
    host: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_host(monkeypatch, host)
    _forbid_gh_probe(monkeypatch)

    with pytest.raises(PrescriptiveError) as excinfo:
        _resolve_narrative_source(
            (), tmp_path, tasks_mined=True, pr_bodies={}
        )

    assert "GitHub-only" in excinfo.value.message
    assert host in excinfo.value.message


def test_github_host_keeps_existing_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GitHub remotes with no merged PRs still get the squash-only diagnosis."""
    _patch_host(monkeypatch, "github")
    monkeypatch.setattr(
        "codeprobe.mining.sources.has_pr_narratives",
        lambda path: False,
    )

    with pytest.raises(PrescriptiveError) as excinfo:
        _resolve_narrative_source(
            (), tmp_path, tasks_mined=True, pr_bodies={}
        )

    exc = excinfo.value
    assert exc.code == "NARRATIVE_SOURCE_UNDETECTABLE"
    assert "squash-only or no-remote history" in exc.message
    assert "GitHub-only" not in exc.message


def test_explicit_commits_selection_bypasses_host_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--narrative-source commits works unchanged on a GitLab remote."""
    _patch_host(monkeypatch, "gitlab")
    _forbid_gh_probe(monkeypatch)

    resolved = _resolve_narrative_source(
        ("commits",), tmp_path, tasks_mined=True, pr_bodies={}
    )

    assert resolved == ("commits",)


def test_local_host_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local repos behave as today: gh probe runs, squash-only message on miss."""
    _patch_host(monkeypatch, "local")
    monkeypatch.setattr(
        "codeprobe.mining.sources.has_pr_narratives",
        lambda path: False,
    )

    with pytest.raises(PrescriptiveError) as excinfo:
        _resolve_narrative_source(
            (), tmp_path, tasks_mined=True, pr_bodies={}
        )
    assert "squash-only or no-remote history" in excinfo.value.message

    monkeypatch.setattr(
        "codeprobe.mining.sources.has_pr_narratives",
        lambda path: True,
    )
    assert _resolve_narrative_source(
        (), tmp_path, tasks_mined=True, pr_bodies={}
    ) == ("pr",)
