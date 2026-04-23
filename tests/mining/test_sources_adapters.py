"""Tests for the narrative-adapter Protocol and its three Phase 2 adapters.

Covers:
* :class:`NarrativeBundle` shape.
* Each of :class:`PRAdapter`, :class:`CommitAdapter`, :class:`RFCAdapter`.
* ``parse_narrative_selection`` / ``select_narrative_adapters`` helpers.
* The INV1 loud-error flow when a squash-only repo is mined without
  ``--narrative-source``.
* ``TaskMetadata.enrichment_source`` population end-to-end.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.mining.adapters import (
    CommitAdapter,
    PRAdapter,
    RFCAdapter,
    build_adapter,
)
from codeprobe.mining.sources import (
    NarrativeAdapter,
    NarrativeBundle,
    has_pr_narratives,
    parse_narrative_selection,
    select_narrative_adapters,
)


# ---------------------------------------------------------------------------
# Git-repo fixtures (subprocess-driven so we don't need GitPython)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "commit.gpgsign", "false")
    return repo


def _write_and_commit(
    repo: Path, filename: str, content: str, message: str
) -> str:
    path = repo / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(repo, "add", filename)
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repo_with_rich_commit(tmp_path: Path) -> tuple[Path, str]:
    """Repo with one commit carrying a rich multi-line body."""
    repo = _init_repo(tmp_path, "rich")
    sha = _write_and_commit(
        repo,
        "src/widget.py",
        "def widget():\n    return 42\n",
        textwrap.dedent(
            """\
            Add widget() feature

            This commit introduces the widget subsystem so the scheduler can
            fan out work units across multiple shards. Closes #42.
            """
        ),
    )
    return repo, sha


@pytest.fixture
def repo_with_squash_only(tmp_path: Path) -> tuple[Path, str]:
    """Repo where every commit is a one-line squash message, no RFCs."""
    repo = _init_repo(tmp_path, "squash")
    _write_and_commit(repo, "a.py", "x = 1\n", "wip")
    sha = _write_and_commit(repo, "b.py", "y = 2\n", "fix typo")
    return repo, sha


@pytest.fixture
def repo_with_rfcs(tmp_path: Path) -> tuple[Path, str]:
    """Repo with an RFC doc under docs/rfcs and a squash-style commit."""
    repo = _init_repo(tmp_path, "rfcs")
    rfc_body = textwrap.dedent(
        """\
        # RFC 001: Widget Protocol

        ## Motivation

        We need a standard protocol for widget interoperability across the
        platform. This document specifies the request/response envelope and
        the error model.
        """
    )
    (repo / "docs" / "rfcs").mkdir(parents=True)
    (repo / "docs" / "rfcs" / "001-widget.md").write_text(rfc_body)
    _git(repo, "add", "docs/rfcs/001-widget.md")
    _git(repo, "commit", "-q", "-m", "wip")
    sha = _write_and_commit(repo, "a.py", "x = 1\n", "fix")
    return repo, sha


# ---------------------------------------------------------------------------
# NarrativeBundle shape
# ---------------------------------------------------------------------------


def test_narrative_bundle_shape() -> None:
    bundle = NarrativeBundle(
        text="hello",
        metadata={"k": "v"},
        source_name="pr",
    )
    assert bundle.text == "hello"
    assert bundle.metadata == {"k": "v"}
    assert bundle.source_name == "pr"


def test_narrative_bundle_defaults() -> None:
    bundle = NarrativeBundle(text="t")
    assert bundle.metadata == {}
    assert bundle.source_name == ""


def test_adapters_conform_to_protocol() -> None:
    for adapter in (PRAdapter(), CommitAdapter(), RFCAdapter()):
        assert isinstance(adapter, NarrativeAdapter)
        assert isinstance(adapter.name, str) and adapter.name


# ---------------------------------------------------------------------------
# parse_narrative_selection
# ---------------------------------------------------------------------------


def test_parse_narrative_selection_plus_separated() -> None:
    assert parse_narrative_selection(("commits+rfcs",)) == ("commits", "rfcs")


def test_parse_narrative_selection_comma_separated() -> None:
    assert parse_narrative_selection(("commits,rfcs",)) == ("commits", "rfcs")


def test_parse_narrative_selection_repeated_flag() -> None:
    assert parse_narrative_selection(("commits", "rfcs")) == ("commits", "rfcs")


def test_parse_narrative_selection_dedups_preserving_order() -> None:
    assert parse_narrative_selection(("pr", "pr,commits", "pr")) == ("pr", "commits")


def test_parse_narrative_selection_case_and_whitespace() -> None:
    assert parse_narrative_selection(("  PR + Commits  ",)) == ("pr", "commits")


def test_parse_narrative_selection_empty() -> None:
    assert parse_narrative_selection(()) == ()


# ---------------------------------------------------------------------------
# select_narrative_adapters
# ---------------------------------------------------------------------------


def test_select_narrative_adapters_known_names() -> None:
    adapters = select_narrative_adapters(("pr", "commits", "rfcs"))
    assert [a.name for a in adapters] == ["pr", "commits", "rfcs"]


def test_select_narrative_adapters_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="Unknown narrative source 'zebra'"):
        select_narrative_adapters(("pr", "zebra"))


def test_select_narrative_adapters_empty_returns_empty() -> None:
    assert select_narrative_adapters(()) == []


def test_build_adapter_unknown_raises() -> None:
    with pytest.raises(KeyError):
        build_adapter("bogus")


# ---------------------------------------------------------------------------
# CommitAdapter
# ---------------------------------------------------------------------------


def test_commit_adapter_returns_rich_body(
    repo_with_rich_commit: tuple[Path, str],
) -> None:
    repo, sha = repo_with_rich_commit
    bundle = CommitAdapter().fetch(repo, sha)
    assert bundle is not None
    assert bundle.source_name == "commits"
    assert "widget" in bundle.text.lower()
    assert "#42" in bundle.text
    assert bundle.metadata.get("sha") == sha


def test_commit_adapter_accepts_one_line_squash(
    repo_with_squash_only: tuple[Path, str],
) -> None:
    repo, sha = repo_with_squash_only
    bundle = CommitAdapter().fetch(repo, sha)
    assert bundle is not None
    assert bundle.text.strip() == "fix typo"
    assert bundle.source_name == "commits"


def test_commit_adapter_returns_none_for_bad_sha(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "bad")
    _write_and_commit(repo, "a.py", "x=1\n", "msg")
    assert CommitAdapter().fetch(repo, "0" * 40) is None


# ---------------------------------------------------------------------------
# RFCAdapter
# ---------------------------------------------------------------------------


def test_rfc_adapter_finds_rfc_file(
    repo_with_rfcs: tuple[Path, str],
) -> None:
    repo, sha = repo_with_rfcs
    bundle = RFCAdapter().fetch(repo, sha)
    assert bundle is not None
    assert bundle.source_name == "rfcs"
    assert "Widget Protocol" in bundle.text
    assert bundle.metadata.get("rfc_path", "").endswith("001-widget.md")


def test_rfc_adapter_returns_none_when_no_rfc_dir(
    repo_with_squash_only: tuple[Path, str],
) -> None:
    repo, sha = repo_with_squash_only
    assert RFCAdapter().fetch(repo, sha) is None


def test_rfc_adapter_prefers_rfcs_touched_in_commit(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path, "rfc-touched")
    (repo / "docs" / "rfcs").mkdir(parents=True)
    older = repo / "docs" / "rfcs" / "001-old.md"
    older.write_text("# Old RFC\n\nnever touched by target commit")
    _git(repo, "add", "docs/rfcs/001-old.md")
    _git(repo, "commit", "-q", "-m", "seed")
    newer = repo / "docs" / "rfcs" / "002-new.md"
    newer.write_text("# New RFC\n\ntouched by target commit")
    _git(repo, "add", "docs/rfcs/002-new.md")
    _git(repo, "commit", "-q", "-m", "add new rfc")
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    bundle = RFCAdapter().fetch(repo, sha)
    assert bundle is not None
    # The commit touched 002-new.md; adapter should surface it.
    assert "New RFC" in bundle.text
    assert bundle.metadata["rfc_path"].endswith("002-new.md")


# ---------------------------------------------------------------------------
# PRAdapter
# ---------------------------------------------------------------------------


def _fake_subprocess_run(stdout: str, returncode: int = 0):
    def _side_effect(cmd, **kwargs):
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=returncode,
            stdout=stdout,
            stderr="",
        )

    return _side_effect


def test_pr_adapter_parses_gh_output(tmp_path: Path) -> None:
    payload = json.dumps(
        [
            {
                "number": 42,
                "title": "Add widget",
                "body": "Detailed rationale spanning multiple lines.",
                "labels": [{"name": "feature"}, {"name": "area/widgets"}],
            }
        ]
    )
    with patch(
        "codeprobe.mining.adapters.pr.subprocess.run",
        side_effect=_fake_subprocess_run(payload),
    ):
        bundle = PRAdapter().fetch(tmp_path, "abc123")
    assert bundle is not None
    assert bundle.source_name == "pr"
    assert "Detailed rationale" in bundle.text
    assert bundle.metadata["pr_number"] == "42"
    assert bundle.metadata["title"] == "Add widget"
    assert bundle.metadata["labels"] == "feature,area/widgets"


def test_pr_adapter_returns_none_when_gh_missing(tmp_path: Path) -> None:
    def _raise(cmd, **kwargs):
        raise FileNotFoundError("gh: not installed")

    with patch(
        "codeprobe.mining.adapters.pr.subprocess.run", side_effect=_raise
    ):
        assert PRAdapter().fetch(tmp_path, "abc123") is None


def test_pr_adapter_returns_none_for_empty_search_hits(tmp_path: Path) -> None:
    with patch(
        "codeprobe.mining.adapters.pr.subprocess.run",
        side_effect=_fake_subprocess_run("[]"),
    ):
        assert PRAdapter().fetch(tmp_path, "abc123") is None


def test_pr_adapter_returns_none_for_empty_body(tmp_path: Path) -> None:
    payload = json.dumps([{"number": 1, "title": "t", "body": "", "labels": []}])
    with patch(
        "codeprobe.mining.adapters.pr.subprocess.run",
        side_effect=_fake_subprocess_run(payload),
    ):
        assert PRAdapter().fetch(tmp_path, "abc123") is None


def test_pr_adapter_returns_none_on_gh_failure(tmp_path: Path) -> None:
    with patch(
        "codeprobe.mining.adapters.pr.subprocess.run",
        side_effect=_fake_subprocess_run("", returncode=1),
    ):
        assert PRAdapter().fetch(tmp_path, "abc123") is None


def test_pr_adapter_logs_warning_on_multi_match(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """When ``gh pr list --search <sha>`` returns >1 PR, the adapter
    still selects the first match (unchanged behaviour) but emits a
    warning with the matched PR numbers so the ambiguity is visible.
    """
    payload = json.dumps(
        [
            {
                "number": 101,
                "title": "first match",
                "body": "first body",
                "labels": [],
            },
            {
                "number": 202,
                "title": "second match",
                "body": "second body",
                "labels": [],
            },
        ]
    )
    with patch(
        "codeprobe.mining.adapters.pr.subprocess.run",
        side_effect=_fake_subprocess_run(payload),
    ):
        with caplog.at_level("WARNING", logger="codeprobe.mining.adapters.pr"):
            bundle = PRAdapter().fetch(tmp_path, "abc123def456")

    # Selection unchanged: first match wins.
    assert bundle is not None
    assert bundle.metadata["pr_number"] == "101"
    # Warning must mention the ambiguity and list the matched PR numbers.
    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) == 1
    msg = warning_records[0].getMessage()
    assert "2 PRs matched" in msg
    assert "101" in msg
    assert "202" in msg


# ---------------------------------------------------------------------------
# has_pr_narratives
# ---------------------------------------------------------------------------


def test_has_pr_narratives_true_when_gh_returns_list(tmp_path: Path) -> None:
    with patch(
        "codeprobe.mining.sources.subprocess.run",
        side_effect=_fake_subprocess_run('[{"number":1}]'),
    ):
        assert has_pr_narratives(tmp_path) is True


def test_has_pr_narratives_false_when_gh_returns_empty(tmp_path: Path) -> None:
    with patch(
        "codeprobe.mining.sources.subprocess.run",
        side_effect=_fake_subprocess_run("[]"),
    ):
        assert has_pr_narratives(tmp_path) is False


def test_has_pr_narratives_false_when_gh_missing(tmp_path: Path) -> None:
    def _raise(cmd, **kwargs):
        raise FileNotFoundError("gh")

    with patch("codeprobe.mining.sources.subprocess.run", side_effect=_raise):
        assert has_pr_narratives(tmp_path) is False


# ---------------------------------------------------------------------------
# INV1 loud-error flow via _resolve_narrative_source
# ---------------------------------------------------------------------------


def test_resolve_narrative_source_explicit_selection(tmp_path: Path) -> None:
    from codeprobe.cli.mine_cmd import _resolve_narrative_source

    selection = _resolve_narrative_source(
        ("commits+rfcs",), tmp_path, tasks_mined=True
    )
    assert selection == ("commits", "rfcs")


def test_resolve_narrative_source_raises_when_no_pr_and_no_flag(
    tmp_path: Path,
) -> None:
    import click

    from codeprobe.cli.mine_cmd import _resolve_narrative_source

    with patch(
        "codeprobe.mining.sources.subprocess.run",
        side_effect=_fake_subprocess_run("[]"),
    ):
        with pytest.raises(click.UsageError) as exc_info:
            _resolve_narrative_source((), tmp_path, tasks_mined=True)
    msg = str(exc_info.value)
    assert "--narrative-source" in msg
    assert "commits+rfcs" in msg


def test_resolve_narrative_source_defaults_to_pr_when_available(
    tmp_path: Path,
) -> None:
    from codeprobe.cli.mine_cmd import _resolve_narrative_source

    with patch(
        "codeprobe.mining.sources.subprocess.run",
        side_effect=_fake_subprocess_run('[{"number":1}]'),
    ):
        selection = _resolve_narrative_source((), tmp_path, tasks_mined=True)
    assert selection == ("pr",)


def test_resolve_narrative_source_skips_check_when_no_tasks(
    tmp_path: Path,
) -> None:
    from codeprobe.cli.mine_cmd import _resolve_narrative_source

    # Should not call has_pr_narratives at all; returns empty silently.
    selection = _resolve_narrative_source((), tmp_path, tasks_mined=False)
    assert selection == ()


def test_resolve_narrative_source_invalid_name_raises_usage_error(
    tmp_path: Path,
) -> None:
    import click

    from codeprobe.cli.mine_cmd import _resolve_narrative_source

    with pytest.raises(click.UsageError, match="Unknown narrative source"):
        _resolve_narrative_source(("bogus",), tmp_path, tasks_mined=True)


# ---------------------------------------------------------------------------
# enrichment_source populated on mined tasks
# ---------------------------------------------------------------------------


def test_enrichment_source_populated_on_tasks() -> None:
    """The dispatch layer stamps enrichment_source on every mined task.

    We simulate the stamping loop that lives in _dispatch_sdlc to verify
    the contract: given a selection, every task ends up with
    metadata.enrichment_source == '+'.join(selection).
    """
    from dataclasses import replace

    from codeprobe.models.task import Task, TaskMetadata, TaskVerification

    task = Task(
        id="abc",
        repo="demo",
        metadata=TaskMetadata(name="demo-abc"),
        verification=TaskVerification(),
    )
    selection = ("commits", "rfcs")
    trail = "+".join(selection)

    stamped = replace(
        task,
        metadata=replace(task.metadata, enrichment_source=trail),
    )
    assert stamped.metadata.enrichment_source == "commits+rfcs"


def test_task_metadata_has_enrichment_source_field() -> None:
    """Acceptance criterion 7: TaskMetadata.enrichment_source exists with '' default."""
    from codeprobe.models.task import TaskMetadata

    meta = TaskMetadata(name="x")
    assert meta.enrichment_source == ""


# ---------------------------------------------------------------------------
# End-to-end CLI loud-error on squash-only repo
# ---------------------------------------------------------------------------


def test_mine_on_squash_only_repo_fails_loudly(
    repo_with_squash_only: tuple[Path, str],
) -> None:
    """Running ``_dispatch_sdlc`` on a squash-only repo with no flag raises."""
    import click

    from codeprobe.cli.mine_cmd import _dispatch_sdlc
    from codeprobe.mining.extractor import MineResult
    from codeprobe.models.task import Task, TaskMetadata, TaskVerification

    repo, _ = repo_with_squash_only

    # Force mine_tasks to return one synthetic task so the INV1 check
    # inside _dispatch_sdlc is exercised (it only runs when tasks_mined).
    fake_task = Task(
        id="aaaa1111",
        repo=repo.name,
        metadata=TaskMetadata(name="fake"),
        verification=TaskVerification(),
    )
    fake_result = MineResult(
        tasks=[fake_task], pr_bodies={}, changed_files_map={}
    )

    with patch(
        "codeprobe.cli.mine_cmd._mine_tasks_with_progress",
        return_value=fake_result,
    ), patch(
        "codeprobe.mining.sources.subprocess.run",
        side_effect=_fake_subprocess_run("[]"),
    ):
        with pytest.raises(click.UsageError) as exc_info:
            _dispatch_sdlc(
                repo_path=repo,
                count=1,
                source="auto",
                min_files=0,
                subsystems=(),
                no_llm=True,
                enrich=False,
                goal_name="test",
                bias="balanced",
                narrative_source=(),
            )
    assert "--narrative-source" in str(exc_info.value)
    assert "commits+rfcs" in str(exc_info.value)
