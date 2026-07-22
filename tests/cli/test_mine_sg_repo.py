"""Tests for SDLC mining ``metadata.sg_repo`` population (codeprobe-evjr.3).

Regression: cross-rig audit (``docs/investigations/codeprobe-evjr/``)
discovered that gascity SDLC tasks were shipping with
``metadata.sg_repo = ""``. The Sourcegraph preamble template renders
``repo:^{{sg_repo}}$ <query>`` → ``repo:^$ <query>``, which is a malformed
scope and silently falls back to global search — inflating cost and
diluting recall.

The fix has two parts; this file covers Part A (mining-side population).
Part B (the ``task_preamble_context`` fail-loud guard) is covered by
``tests/test_preamble.py::test_task_preamble_context_raises_*``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from codeprobe.cli.mine_cmd import _resolve_origin_sg_repo, _stamp_sg_repo
from codeprobe.models.task import Task, TaskMetadata


def _make_task(name: str, sg_repo: str = "") -> Task:
    return Task(
        id=name,
        repo="repo",
        metadata=TaskMetadata(name=name, sg_repo=sg_repo),
    )


# ---------------------------------------------------------------------------
# _resolve_origin_sg_repo
# ---------------------------------------------------------------------------


def test_resolve_origin_sg_repo_uses_explicit_value(tmp_path):
    """Explicit ``--sg-repo`` always wins over origin detection."""
    repo = tmp_path / "repo"
    repo.mkdir()

    with patch(
        "codeprobe.mining.sources.detect_source"
    ) as mock_detect:
        result = _resolve_origin_sg_repo(
            repo, explicit_sg_repo="github.com/user/explicit"
        )

    assert result == "github.com/user/explicit"
    # The explicit branch must short-circuit before touching the source detector.
    mock_detect.assert_not_called()


def test_resolve_origin_sg_repo_falls_back_to_origin(tmp_path):
    """When no explicit ``--sg-repo`` is passed, derive from the origin remote."""
    repo = tmp_path / "repo"
    repo.mkdir()

    fake_source = type(
        "FakeSource",
        (),
        {"remote_url": "https://github.com/gastownhall/gascity.git"},
    )()

    with patch(
        "codeprobe.mining.sources.detect_source", return_value=fake_source
    ):
        result = _resolve_origin_sg_repo(repo, explicit_sg_repo="")

    assert result == "github.com/gastownhall/gascity"


def test_resolve_origin_sg_repo_returns_empty_when_origin_missing(tmp_path):
    """Repos without a parseable remote yield an empty ``sg_repo``.

    The downstream ``task_preamble_context`` guard fails loud if a
    Sourcegraph preamble is requested without ``sg_repo`` populated, so
    silent empties remain safe at mine time.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    fake_source = type("FakeSource", (), {"remote_url": ""})()

    with patch(
        "codeprobe.mining.sources.detect_source", return_value=fake_source
    ):
        result = _resolve_origin_sg_repo(repo, explicit_sg_repo="")

    assert result == ""


# ---------------------------------------------------------------------------
# _stamp_sg_repo
# ---------------------------------------------------------------------------


def test_stamp_sg_repo_populates_metadata():
    """Every task in the list receives the sg_repo identifier."""
    tasks = [_make_task("t1"), _make_task("t2")]

    stamped = _stamp_sg_repo(tasks, "github.com/owner/repo")

    assert all(t.metadata.sg_repo == "github.com/owner/repo" for t in stamped)
    # Original tasks remain unmutated (frozen dataclass invariant).
    assert all(t.metadata.sg_repo == "" for t in tasks)


def test_stamp_sg_repo_is_noop_when_empty():
    """Empty ``sg_repo`` short-circuits and returns the original list.

    This preserves backwards compatibility for offline mining flows and
    test fixtures that don't carry a remote URL.
    """
    tasks = [_make_task("t1", sg_repo="github.com/old/value")]

    stamped = _stamp_sg_repo(tasks, "")

    assert stamped is tasks
    assert stamped[0].metadata.sg_repo == "github.com/old/value"


def test_stamp_sg_repo_overwrites_existing_value():
    """A non-empty stamp value replaces any pre-existing sg_repo on the task."""
    tasks = [_make_task("t1", sg_repo="github.com/stale/value")]

    stamped = _stamp_sg_repo(tasks, "github.com/owner/repo")

    assert stamped[0].metadata.sg_repo == "github.com/owner/repo"


def test_stamp_sg_repo_preserves_task_count():
    """Output list cardinality matches input even for empty inputs."""
    assert _stamp_sg_repo([], "github.com/owner/repo") == []
    tasks = [_make_task(f"t{i}") for i in range(5)]
    assert len(_stamp_sg_repo(tasks, "github.com/owner/repo")) == 5


@pytest.mark.parametrize(
    "remote,expected",
    [
        (
            "https://github.com/gastownhall/gascity.git",
            "github.com/gastownhall/gascity",
        ),
        (
            "git@github.com:owner/repo.git",
            "github.com/owner/repo",
        ),
        ("", ""),
    ],
)
def test_resolve_then_stamp_end_to_end(tmp_path, remote, expected):
    """End-to-end Part A flow: detect remote, derive sg_repo, stamp tasks."""
    repo = tmp_path / "repo"
    repo.mkdir()

    fake_source = type("FakeSource", (), {"remote_url": remote})()

    with patch(
        "codeprobe.mining.sources.detect_source", return_value=fake_source
    ):
        derived = _resolve_origin_sg_repo(repo, explicit_sg_repo="")

    tasks = [_make_task("t1"), _make_task("t2")]
    stamped = _stamp_sg_repo(tasks, derived)

    if expected:
        assert all(t.metadata.sg_repo == expected for t in stamped)
    else:
        # Empty derivation → no-op; the original tasks pass through unchanged.
        assert stamped is tasks


# ---------------------------------------------------------------------------
# _run_org_scale_mine sg_repo derivation (codeprobe-f7rl.23)
# ---------------------------------------------------------------------------
#
# Regression: the mcp-families path used to default sg_repo to the
# maintainer's org (github.com/sg-evals/{name}) — tribal knowledge baked
# into code that scoped a customer's Sourcegraph queries to a repo that
# doesn't exist for them. It now derives from the origin remote like the
# SDLC path, and hard-refuses (SG_REPO_UNRESOLVED) when the origin is
# missing, because an empty sg_repo silently disables the Sourcegraph leg.


def _fake_source(remote_url: str):
    return type("FakeSource", (), {"remote_url": remote_url})()


def _empty_result():
    from unittest.mock import MagicMock

    result = MagicMock()
    result.tasks = []
    result.scan_results = []
    return result


def _run_org_scale(repo, **kwargs):
    from codeprobe.cli.mine_cmd import _run_org_scale_mine

    return _run_org_scale_mine([repo], count=1, no_llm=True, **kwargs)


def test_mcp_families_sg_repo_derived_from_origin(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    with (
        patch(
            "codeprobe.mining.org_scale.mine_org_scale_tasks",
            return_value=_empty_result(),
        ) as mock_mine,
        patch(
            "codeprobe.mining.sources.detect_source",
            return_value=_fake_source("https://github.com/acme/widgets.git"),
        ),
        patch("codeprobe.cli.mine_cmd._is_interactive", return_value=False),
    ):
        _run_org_scale(repo, mcp_families=True)

    mock_mine.assert_called_once()
    sg_repo = mock_mine.call_args.kwargs["sg_repo"]
    assert sg_repo == "github.com/acme/widgets"
    assert "sg-evals" not in sg_repo


def test_mcp_families_explicit_sg_repo_wins(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    with (
        patch(
            "codeprobe.mining.org_scale.mine_org_scale_tasks",
            return_value=_empty_result(),
        ) as mock_mine,
        patch("codeprobe.mining.sources.detect_source") as mock_detect,
        patch("codeprobe.cli.mine_cmd._is_interactive", return_value=False),
    ):
        _run_org_scale(repo, mcp_families=True, sg_repo="github.com/acme/x")

    mock_mine.assert_called_once()
    assert mock_mine.call_args.kwargs["sg_repo"] == "github.com/acme/x"
    mock_detect.assert_not_called()


def test_mcp_families_no_origin_raises(tmp_path):
    from codeprobe.cli.errors import PrescriptiveError

    repo = tmp_path / "repo"
    repo.mkdir()

    with (
        patch(
            "codeprobe.mining.org_scale.mine_org_scale_tasks",
            return_value=_empty_result(),
        ) as mock_mine,
        patch(
            "codeprobe.mining.sources.detect_source",
            return_value=_fake_source(""),
        ),
        patch("codeprobe.cli.mine_cmd._is_interactive", return_value=False),
        pytest.raises(PrescriptiveError) as excinfo,
    ):
        _run_org_scale(repo, mcp_families=True)

    assert excinfo.value.code == "SG_REPO_UNRESOLVED"
    assert excinfo.value.next_try_flag == "--sg-repo"
    assert excinfo.value.exit_code == 2
    mock_mine.assert_not_called()


def test_non_mcp_org_scale_unaffected(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()

    with (
        patch(
            "codeprobe.mining.org_scale.mine_org_scale_tasks",
            return_value=_empty_result(),
        ) as mock_mine,
        patch("codeprobe.mining.sources.detect_source") as mock_detect,
        patch("codeprobe.cli.mine_cmd._is_interactive", return_value=False),
    ):
        _run_org_scale(repo, mcp_families=False)

    mock_mine.assert_called_once()
    assert mock_mine.call_args.kwargs["sg_repo"] == ""
    mock_detect.assert_not_called()
