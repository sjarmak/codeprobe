"""Class E fairness scanner tests.

Covers the static (agent-facing files) and dynamic (rendered preamble) sides
of :func:`codeprobe.snapshot.fairness.check_fairness`, plus the CI gate that
hooks it into :func:`codeprobe.snapshot.create.create_snapshot`.

Each test builds a small task corpus on tmp_path so the suite never depends on
the live codeprobe corpus state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from codeprobe.snapshot.create import FairnessLeakError, create_snapshot
from codeprobe.snapshot.fairness import (
    FairnessLeak,
    FairnessResult,
    check_fairness,
    discover_agent_facing_files,
    discover_task_dirs,
    write_fairness_report,
)
from codeprobe.snapshot.verify import (
    check_fairness as verify_check_fairness,
)


def _write_task(
    parent: Path,
    task_id: str,
    *,
    expected: list[str] | None = None,
    answer_type: str | None = None,
    answer: object | None = None,
) -> Path:
    """Create a minimal codeprobe task dir under ``parent`` and return it."""
    task_dir = parent / task_id
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "instruction.md").write_text("# Task\n\nDo a thing.\n")

    payload: dict = {"schema_version": 1}
    if expected is not None:
        payload["oracle_type"] = "file_list"
        payload["expected"] = expected
    if answer_type is not None:
        payload["answer_type"] = answer_type
        payload["answer"] = answer

    (task_dir / "tests" / "ground_truth.json").write_text(
        json.dumps(payload, indent=2)
    )
    return task_dir


def _write_repo_root(repo_root: Path, *, claude_md: str = "", agents_md: str = "",
                     readme: str = "", cursor_rule: str = "") -> None:
    """Populate a fake repo root with agent-facing files."""
    repo_root.mkdir(parents=True, exist_ok=True)
    if claude_md:
        (repo_root / "CLAUDE.md").write_text(claude_md)
    if agents_md:
        (repo_root / "AGENTS.md").write_text(agents_md)
    if readme:
        (repo_root / "README.md").write_text(readme)
    if cursor_rule:
        cursor_dir = repo_root / ".cursor" / "rules"
        cursor_dir.mkdir(parents=True, exist_ok=True)
        (cursor_dir / "default.mdc").write_text(cursor_rule)


def test_clean_corpus_zero_leaks(tmp_path: Path) -> None:
    """A corpus with no agent-facing files cannot leak."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_task(corpus, "t1", expected=["src/foo.py"])

    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # No CLAUDE.md / AGENTS.md / README — nothing to scan.

    result = check_fairness(task_roots=[corpus], repo_root=repo_root)

    assert result.ok is True
    assert result.tasks_scanned == 1
    assert result.aux_files_scanned == 0
    assert result.leaks == []


def test_clean_repo_with_unrelated_paths(tmp_path: Path) -> None:
    """Agent-facing files that mention unrelated paths don't trigger leaks."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_task(corpus, "t1", expected=["src/foo.py"])

    repo_root = tmp_path / "repo"
    _write_repo_root(
        repo_root,
        claude_md="See `src/bar.py` for examples.\n",
        readme="# Project\n\nSomething about `src/baz.py`.\n",
    )

    result = check_fairness(task_roots=[corpus], repo_root=repo_root)

    assert result.ok is True
    assert result.tasks_scanned == 1
    assert result.aux_files_scanned == 2
    assert result.leaks == []


def test_leak_in_claude_md(tmp_path: Path) -> None:
    """An oracle path mentioned in CLAUDE.md is reported as a static leak."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_task(corpus, "leaky-task", expected=["src/secret/answer.py"])

    repo_root = tmp_path / "repo"
    _write_repo_root(
        repo_root,
        claude_md="See `src/secret/answer.py` for the canonical example.\n",
    )

    result = check_fairness(task_roots=[corpus], repo_root=repo_root)

    assert result.ok is False
    assert result.tasks_scanned == 1
    assert result.aux_files_scanned == 1
    assert len(result.leaks) == 1
    leak = result.leaks[0]
    assert leak.task_id == "leaky-task"
    assert leak.token == "src/secret/answer.py"
    assert leak.kind == "static"
    assert "CLAUDE.md" in leak.location


def test_leak_in_agents_md(tmp_path: Path) -> None:
    """AGENTS.md is treated as agent-facing too."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_task(corpus, "t1", expected=["src/secret/leaked.py"])

    repo_root = tmp_path / "repo"
    _write_repo_root(
        repo_root,
        agents_md="The relevant file is `src/secret/leaked.py`.",
    )

    result = check_fairness(task_roots=[corpus], repo_root=repo_root)
    assert result.ok is False
    assert any("AGENTS.md" in leak.location for leak in result.leaks)


def test_leak_in_cursor_rules(tmp_path: Path) -> None:
    """Files under .cursor/ are scanned as agent-facing."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_task(corpus, "t1", expected=["src/secret/cursor_leak.py"])

    repo_root = tmp_path / "repo"
    _write_repo_root(
        repo_root,
        cursor_rule="When working on `src/secret/cursor_leak.py`, ...",
    )

    result = check_fairness(task_roots=[corpus], repo_root=repo_root)
    assert result.ok is False
    assert any(".cursor" in leak.location for leak in result.leaks)


def test_short_tokens_filtered(tmp_path: Path) -> None:
    """Tokens shorter than 3 chars must not produce hits — too noisy."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_task(corpus, "t1", answer_type="count", answer=3)
    _write_task(corpus, "t2", answer_type="text", answer="X")

    repo_root = tmp_path / "repo"
    _write_repo_root(
        repo_root,
        claude_md="The number 3 appears here. Variable X also appears.",
    )

    result = check_fairness(task_roots=[corpus], repo_root=repo_root)
    assert result.ok is True


def test_generic_tokens_filtered(tmp_path: Path) -> None:
    """`true`, `False`, `None` etc. must not produce false-positive hits."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_task(corpus, "t1", answer_type="boolean", answer=True)
    _write_task(corpus, "t2", answer_type="text", answer="None")

    repo_root = tmp_path / "repo"
    _write_repo_root(
        repo_root,
        claude_md="The expression evaluates to True if the value is None.",
    )

    result = check_fairness(task_roots=[corpus], repo_root=repo_root)
    assert result.ok is True


def test_preamble_leak(tmp_path: Path) -> None:
    """Oracle tokens appearing in a rendered preamble are flagged."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_task(corpus, "t1", expected=["src/preamble_leak.py"])

    repo_root = tmp_path / "repo"

    rendered = {
        "test_preamble.md": (
            "# Tools\n\nUse the search_keyword tool to find "
            "`src/preamble_leak.py` in the repo.\n"
        )
    }

    result = check_fairness(
        task_roots=[corpus],
        repo_root=repo_root,
        rendered_preambles=rendered,
    )

    assert result.ok is False
    assert result.preambles_scanned == 1
    assert any(leak.kind == "preamble" for leak in result.leaks)
    preamble_leak = next(leak for leak in result.leaks if leak.kind == "preamble")
    assert preamble_leak.location == "test_preamble.md"
    assert preamble_leak.token == "src/preamble_leak.py"


def test_skip_repo_walk_uses_extra_files_only(tmp_path: Path) -> None:
    """``skip_repo_walk`` lets callers scan only an explicit file list."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_task(corpus, "t1", expected=["src/extra_only.py"])

    repo_root = tmp_path / "repo"
    _write_repo_root(
        repo_root,
        claude_md="Mentions `src/extra_only.py` but should NOT be scanned.",
    )

    extra = tmp_path / "extra.md"
    extra.write_text("Mentions `src/extra_only.py` and SHOULD be scanned.")

    result = check_fairness(
        task_roots=[corpus],
        repo_root=repo_root,
        extra_agent_files=[extra],
        skip_repo_walk=True,
    )

    # Repo walk skipped: only the extra file produces a leak.
    assert result.aux_files_scanned == 1
    assert result.ok is False
    assert len(result.leaks) == 1
    assert result.leaks[0].location == str(extra)


def test_extra_files_added_to_repo_walk(tmp_path: Path) -> None:
    """``extra_agent_files`` is unioned with the repo walk, not replacing it."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_task(corpus, "t1", expected=["src/from_repo.py"])
    _write_task(corpus, "t2", expected=["src/from_extra.py"])

    repo_root = tmp_path / "repo"
    _write_repo_root(repo_root, claude_md="Mentions src/from_repo.py here.")

    extra = tmp_path / "remote_claude.md"
    extra.write_text("Mentions src/from_extra.py here.")

    result = check_fairness(
        task_roots=[corpus],
        repo_root=repo_root,
        extra_agent_files=[extra],
    )

    assert result.aux_files_scanned == 2
    assert {leak.task_id for leak in result.leaks} == {"t1", "t2"}


def test_discover_task_dirs_skips_worktrees(tmp_path: Path) -> None:
    """``.claude/worktrees/`` copies must not be double-counted."""
    primary = tmp_path / "primary"
    primary.mkdir()
    _write_task(primary, "t1", expected=["src/foo.py"])

    worktree = tmp_path / ".claude" / "worktrees" / "agent-deadbeef" / "tasks"
    worktree.mkdir(parents=True)
    _write_task(worktree, "t1-copy", expected=["src/foo.py"])

    discovered = discover_task_dirs([tmp_path])
    assert any(d.name == "t1" for d in discovered)
    assert all("worktrees" not in str(d) for d in discovered)


def test_discover_agent_facing_files(tmp_path: Path) -> None:
    """Discovery picks up CLAUDE.md, AGENTS.md, README.md, .cursor/ files."""
    repo = tmp_path / "repo"
    _write_repo_root(
        repo,
        claude_md="claude content",
        agents_md="agents content",
        readme="readme content",
        cursor_rule="cursor content",
    )

    discovered = discover_agent_facing_files(repo)
    names = {p.name for p in discovered}
    assert "CLAUDE.md" in names
    assert "AGENTS.md" in names
    assert "README.md" in names
    assert "default.mdc" in names


def test_check_fairness_re_exported_from_verify() -> None:
    """The bead's acceptance criterion A1 specifies the symbol path."""
    assert verify_check_fairness is check_fairness


def test_write_fairness_report(tmp_path: Path) -> None:
    """``write_fairness_report`` produces a JSON file with the leak summary."""
    leak = FairnessLeak(
        task_id="t1",
        token="src/foo.py",
        location="/path/to/CLAUDE.md",
        kind="static",
    )
    result = FairnessResult(
        ok=False,
        tasks_scanned=1,
        aux_files_scanned=1,
        preambles_scanned=0,
        leaks=[leak],
    )

    out_path = tmp_path / "out" / "fairness.json"
    write_fairness_report(result, out_path)

    assert out_path.is_file()
    payload = json.loads(out_path.read_text())
    assert payload["ok"] is False
    assert payload["leak_count"] == 1
    assert payload["leaks"][0]["task_id"] == "t1"


def test_supports_v1_answer_type_file_list(tmp_path: Path) -> None:
    """The v1 ``answer_type=file_list`` shape is supported."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_task(
        corpus,
        "t1",
        answer_type="file_list",
        answer=["src/v1_format.py"],
    )

    repo_root = tmp_path / "repo"
    _write_repo_root(repo_root, claude_md="See src/v1_format.py.")

    result = check_fairness(task_roots=[corpus], repo_root=repo_root)
    assert result.ok is False
    assert result.leaks[0].token == "src/v1_format.py"


def test_create_snapshot_ci_gate_blocks_leak(tmp_path: Path) -> None:
    """``fairness_check=True`` aborts ``create_snapshot`` when a task leaks."""
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    _write_task(experiment, "t1", expected=["src/blocked.py"])
    # Plant a leak in a CLAUDE.md scanned via fairness_repo_root.
    repo_root = tmp_path / "repo"
    _write_repo_root(
        repo_root,
        claude_md="The answer is `src/blocked.py`.",
    )

    out = tmp_path / "snapshot"
    with pytest.raises(FairnessLeakError, match="src/blocked.py"):
        create_snapshot(
            experiment_dir=experiment,
            out_dir=out,
            fairness_check=True,
            fairness_repo_root=repo_root,
        )
    # The snapshot must not have been written when the gate fired.
    assert not (out / "SNAPSHOT.json").exists()


def test_create_snapshot_ci_gate_passes_clean(tmp_path: Path) -> None:
    """A clean corpus with the gate enabled still produces a snapshot."""
    experiment = tmp_path / "experiment"
    experiment.mkdir()
    _write_task(experiment, "t1", expected=["src/all_clear.py"])
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    # No agent-facing files in repo_root, so nothing can leak.

    out = tmp_path / "snapshot"
    status = create_snapshot(
        experiment_dir=experiment,
        out_dir=out,
        fairness_check=True,
        fairness_repo_root=repo_root,
    )
    assert status["status"] == "ok"
    assert (out / "SNAPSHOT.json").exists()


def test_v2_checks_array_format(tmp_path: Path) -> None:
    """The v2 ``checks`` array shape is supported through token extraction."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    task_dir = corpus / "t1"
    (task_dir / "tests").mkdir(parents=True)
    (task_dir / "tests" / "ground_truth.json").write_text(
        json.dumps(
            {
                "checks": [
                    {
                        "answer_type": "file_list",
                        "answer": ["src/v2_check.py"],
                    }
                ]
            }
        )
    )

    repo_root = tmp_path / "repo"
    _write_repo_root(repo_root, claude_md="See src/v2_check.py.")

    result = check_fairness(task_roots=[corpus], repo_root=repo_root)
    assert result.ok is False
    assert result.leaks[0].token == "src/v2_check.py"
