"""Tests for Slice 1b: verifier in clean checkout via diff materialization.

Bead: codeprobe-xysn. Companion to ``tests/test_scoring_verdict_fields.py``
which covers the additive Slice 1a schema (verdict + materialized_via on
ScoreResult).

Slice 1b adds the behavioural piece: when an agent workspace is a git
repo and a ``base_commit`` was captured by the executor, the verifier
runs against a *fresh checkout* at ``base_commit`` with the agent's full
diff (committed + staged + unstaged + untracked) materialized via
``git apply``. This isolates the test from any dirty-tree pollution
the agent may have left behind AND surfaces "I can't honestly apply
the agent's diff" as a distinct verdict (``verifier_error``) rather
than silently grading the agent as ``incorrect``.

The 5-state outcome contract:

    | apply_check  | test.sh   | verdict          | materialized_via |
    |--------------|-----------|------------------|------------------|
    | ok           | exit 0    | correct          | git_apply        |
    | ok           | exit !=0  | incorrect        | git_apply        |
    | failed       | (skipped) | verifier_error   | git_apply        |
    | no agent_state | (in_place run) | correct/incorrect | in_place    |
    | non-git ws   | (in_place run) | correct/incorrect | in_place        |

See ``docs/scoring_model.md`` §Verifier materialization for the full
table and ``docs/prd/premortem_hybrid_execution_evaluation.md`` Theme C
for the silent-pollution failure mode this slice closes.

This file is intentionally written before the implementation lands —
TDD RED expected at first run. Once ``AgentState`` and the
``agent_state`` kwarg ship on ``BinaryScorer.score``, every test goes
green.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import pytest

from codeprobe.core import scoring
from codeprobe.core.scoring import AgentState, BinaryScorer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(tmp_path: Path, name: str = "ws") -> Path:
    """Initialise a git repo with deterministic identity for commits.

    Uses ``git -c init.defaultBranch=main init`` so older git binaries
    (pre-2.28) that don't recognise ``--initial-branch`` still land on
    ``main``.
    """
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", str(repo)],
        check=True,
        capture_output=True,
    )
    _git(repo, "config", "user.email", "test@codeprobe.local")
    _git(repo, "config", "user.name", "codeprobe-test")
    return repo


def _commit_all(repo: Path, message: str) -> str:
    """Stage every change and commit; return the resulting commit sha."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _init_repo_with_base(
    tmp_path: Path,
    files: dict[str, str],
    *,
    name: str = "ws",
    message: str = "initial",
) -> tuple[Path, str]:
    """Init a repo, write *files* (relpath → content), commit, return (ws, sha).

    Convenience wrapper for the most common test setup: a fresh repo
    with one initial commit. Tests that need additional commits past
    ``base`` keep calling ``_commit_all`` directly.
    """
    repo = _init_repo(tmp_path, name)
    for relpath, content in files.items():
        (repo / relpath).write_text(content)
    return repo, _commit_all(repo, message)


def _make_task_dir(
    tmp_path: Path,
    test_sh: str,
    *,
    name: str = "task",
) -> Path:
    """Build a minimal task_dir with tests/test.sh + verification metadata."""
    task = tmp_path / name
    (task / "tests").mkdir(parents=True)
    (task / "tests" / "test.sh").write_text(test_sh)
    (task / "tests" / "test.sh").chmod(0o755)
    (task / "metadata.json").write_text(
        '{"verification": {"reward_type": "binary"}}'
    )
    return task


# ---------------------------------------------------------------------------
# Scenario 1: committed edits flow through git_apply and score correct
# ---------------------------------------------------------------------------


class TestCommittedEdits:
    """A workspace with a committed agent edit should materialize cleanly."""

    def test_committed_edit_scores_correct_via_git_apply(
        self, tmp_path: Path
    ) -> None:
        ws, base = _init_repo_with_base(
            tmp_path, {"src.py": "def hello(): return 'old'\n"}
        )

        # "Agent" modifies the file and commits.
        (ws / "src.py").write_text("def hello(): return 'new'\n")
        _commit_all(ws, "agent edit")

        # Verifier checks the materialised workspace via TASK_REPO_ROOT.
        task = _make_task_dir(
            tmp_path,
            'grep -q "new" "$TASK_REPO_ROOT/src.py" || exit 1\n',
            name="task-committed",
        )

        scorer = BinaryScorer()
        agent_state = AgentState(base_commit=base, workspace=ws)
        result = scorer.score("", task, agent_state=agent_state)

        assert result.verdict == "correct"
        assert result.materialized_via == "git_apply"
        assert result.score == 1.0
        assert result.passed is True


# ---------------------------------------------------------------------------
# Scenario 2: unstaged + untracked work is captured via git add -A
# ---------------------------------------------------------------------------


class TestUnstagedAndUntrackedEdits:
    """``git add -A`` before diff means we score the agent's full intent.

    Premortem framing said "score committed only" — that would silently
    drop work for agents that never run ``git commit``. Real agents
    (Claude Code, Codex) often leave dirty trees. Slice 1b honours the
    full picture.
    """

    def test_unstaged_modification_is_materialized(self, tmp_path: Path) -> None:
        ws, base = _init_repo_with_base(
            tmp_path, {"src.py": "def hello(): return 'old'\n"}
        )

        # Agent edits but does NOT commit. Tree is dirty at scoring time.
        (ws / "src.py").write_text("def hello(): return 'new'\n")

        task = _make_task_dir(
            tmp_path,
            'grep -q "new" "$TASK_REPO_ROOT/src.py" || exit 1\n',
            name="task-unstaged",
        )

        scorer = BinaryScorer()
        agent_state = AgentState(base_commit=base, workspace=ws)
        result = scorer.score("", task, agent_state=agent_state)

        assert result.verdict == "correct"
        assert result.materialized_via == "git_apply"

    def test_untracked_new_file_is_materialized(self, tmp_path: Path) -> None:
        ws, base = _init_repo_with_base(tmp_path, {"README.md": "placeholder\n"})

        # Agent creates a brand-new untracked file.
        (ws / "new_module.py").write_text("VALUE = 42\n")

        task = _make_task_dir(
            tmp_path,
            'test -f "$TASK_REPO_ROOT/new_module.py" || exit 1\n'
            'grep -q "VALUE = 42" "$TASK_REPO_ROOT/new_module.py" || exit 1\n',
            name="task-untracked",
        )

        scorer = BinaryScorer()
        agent_state = AgentState(base_commit=base, workspace=ws)
        result = scorer.score("", task, agent_state=agent_state)

        assert result.verdict == "correct"
        assert result.materialized_via == "git_apply"


# ---------------------------------------------------------------------------
# Scenario 3: rejected diff routes to verdict='verifier_error', NOT 'incorrect'
# ---------------------------------------------------------------------------


class TestApplyRejected:
    """When ``git apply --check`` fails the verifier must NOT grade the run.

    The premortem flagged this as the single most damaging failure mode:
    a malformed diff (binary corruption, conflict against the captured
    base) gets silently graded as ``incorrect`` and the agent looks bad
    when in fact the verifier infrastructure failed. Slice 1b surfaces
    this distinctly so reviewers can route it to a different bucket.

    This is a white-box test that monkey-patches the private
    ``_capture_workspace_diff`` helper — the cleanest reproducible way
    to force an apply rejection. If the helper is renamed, update this
    test accordingly.
    """

    def test_conflicting_diff_emits_verifier_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws, base = _init_repo_with_base(tmp_path, {"src.py": "hello\n"})
        (ws / "src.py").write_text("agent edit\n")

        task = _make_task_dir(
            tmp_path,
            "exit 0\n",  # would have passed if we got this far
            name="task-rejected",
        )

        def _bad_diff(_workspace: Path, _base: str) -> tuple[bytes, str | None]:
            # A garbage payload that 'git apply --check' refuses.
            return b"this is not a valid unified diff\n", None

        monkeypatch.setattr(scoring, "_capture_workspace_diff", _bad_diff)

        scorer = BinaryScorer()
        agent_state = AgentState(base_commit=base, workspace=ws)
        result = scorer.score("", task, agent_state=agent_state)

        assert result.verdict == "verifier_error"
        # apply_failed lives in the git_apply bucket: we attempted that
        # path, it failed at the apply step. Don't conflate with in_place.
        assert result.materialized_via == "git_apply"
        # Score is 0.0 but passed must NOT be confused for an agent fail.
        assert result.passed is False
        assert result.error is not None
        err = result.error.lower()
        # Surface BOTH the operation name and an indication of what was
        # rejected, so a silently-swallowed stderr would fail this test.
        assert "git apply" in err
        assert ("error" in err) or ("patch" in err) or ("reject" in err)


# ---------------------------------------------------------------------------
# Scenario 4: agent moves HEAD mid-run; scoring uses CAPTURED base
# ---------------------------------------------------------------------------


class TestCapturedBaseSurvivesHeadMovement:
    """The materialisation pipeline must survive an agent that mutates HEAD.

    The honest framing (per the architect review): diff-then-apply is
    mathematically equivalent regardless of whether the diff is computed
    against ``base_commit`` or current ``HEAD`` *as long as both the diff
    base and the checkout base are the same SHA*. The real failure mode
    is an implementation that picks DIFFERENT shas for the two — e.g.,
    diff against ``base_commit`` but checkout at HEAD. That produces an
    apply rejection (the diff was computed against a different tree than
    the checkout it's being applied to).

    This test asserts:
      1. An agent that committed intermediate state and advanced HEAD
         past ``base_commit`` still scores correctly.
      2. The implementation does NOT pick HEAD as the checkout base
         while computing the diff against ``base_commit`` — that mix
         would route to ``verifier_error``, not ``correct``.
    """

    def test_diff_pipeline_survives_intermediate_agent_commits(
        self, tmp_path: Path
    ) -> None:
        # ``to_delete.txt`` is present at base but DELETED later —
        # exercises the rename/delete path that bare ``git diff HEAD``
        # would miss. ``base`` is the SHA the executor captures.
        ws, base = _init_repo_with_base(
            tmp_path,
            {"src.py": "v1\n", "to_delete.txt": "doomed\n"},
        )

        # Agent commits an intermediate state (deletes a file)...
        (ws / "to_delete.txt").unlink()
        (ws / "src.py").write_text("v2\n")
        _commit_all(ws, "intermediate")

        # ...then advances further. HEAD is now well past ``base``.
        (ws / "src.py").write_text("v3-final\n")
        _commit_all(ws, "final")

        # Test asserts the agent's full intent landed: src.py == v3-final
        # AND to_delete.txt is gone. Both pieces of state are reachable
        # only if the diff was computed (and applied to a checkout) that
        # both rooted at ``base``.
        task = _make_task_dir(
            tmp_path,
            'grep -q "v3-final" "$TASK_REPO_ROOT/src.py" || exit 1\n'
            'test ! -e "$TASK_REPO_ROOT/to_delete.txt" || exit 1\n',
            name="task-head-moved",
        )

        scorer = BinaryScorer()
        agent_state = AgentState(base_commit=base, workspace=ws)
        result = scorer.score("", task, agent_state=agent_state)

        assert result.verdict == "correct"
        assert result.materialized_via == "git_apply"


# ---------------------------------------------------------------------------
# Scenario 5: in_place fallback for non-git workspaces
# ---------------------------------------------------------------------------


class TestInPlaceFallback:
    """Tasks whose workspace isn't a git repo keep the legacy behaviour."""

    def test_no_agent_state_uses_in_place(self, tmp_path: Path) -> None:
        # Build a task that passes regardless of TASK_REPO_ROOT —
        # exercising the no-materialization path.
        task = _make_task_dir(tmp_path, "exit 0\n", name="task-no-state")

        scorer = BinaryScorer()
        # No agent_state at all = legacy call.
        result = scorer.score("", task)

        assert result.verdict == "correct"
        assert result.materialized_via == "in_place"
        assert result.score == 1.0
        assert result.passed is True

    def test_non_git_workspace_falls_back_to_in_place(
        self, tmp_path: Path
    ) -> None:
        # A directory that exists but is NOT a git repo.
        ws = tmp_path / "scratch"
        ws.mkdir()
        (ws / "answer.txt").write_text("ok\n")

        task = _make_task_dir(tmp_path, "exit 0\n", name="task-non-git")

        scorer = BinaryScorer()
        agent_state = AgentState(base_commit="deadbeef", workspace=ws)
        result = scorer.score("", task, agent_state=agent_state)

        assert result.verdict == "correct"
        # We fell back because workspace has no .git directory.
        assert result.materialized_via == "in_place"
        assert result.score == 1.0
        assert result.passed is True


# ---------------------------------------------------------------------------
# Scenario 6: test.sh failure under git_apply maps to 'incorrect'
# ---------------------------------------------------------------------------


class TestTestFailureMapsToIncorrect:
    """When materialization succeeds but tests fail, verdict='incorrect'."""

    def test_failing_test_under_git_apply_is_incorrect(
        self, tmp_path: Path
    ) -> None:
        ws, base = _init_repo_with_base(tmp_path, {"src.py": "hello\n"})
        (ws / "src.py").write_text("agent did the wrong thing\n")

        # Test expects "world" — agent wrote something else.
        task = _make_task_dir(
            tmp_path,
            'grep -q "world" "$TASK_REPO_ROOT/src.py" || exit 1\n',
            name="task-wrong",
        )

        scorer = BinaryScorer()
        agent_state = AgentState(base_commit=base, workspace=ws)
        result = scorer.score("", task, agent_state=agent_state)

        assert result.verdict == "incorrect"
        assert result.materialized_via == "git_apply"
        assert result.passed is False


# ---------------------------------------------------------------------------
# Scenario 7: fresh-checkout tempdirs are cleaned up after scoring
# ---------------------------------------------------------------------------


class TestTempdirCleanup:
    """A leaking fresh checkout would fill /tmp under long eval runs."""

    def test_no_lingering_codeprobe_score_tempdirs(self, tmp_path: Path) -> None:
        ws, base = _init_repo_with_base(tmp_path, {"src.py": "hello\n"})
        (ws / "src.py").write_text("changed\n")

        task = _make_task_dir(
            tmp_path,
            'grep -q "changed" "$TASK_REPO_ROOT/src.py" || exit 1\n',
            name="task-cleanup",
        )

        tmp_root = Path(tempfile.gettempdir())
        before = {
            p.name
            for p in tmp_root.iterdir()
            if p.name.startswith("codeprobe-score-")
        }

        scorer = BinaryScorer()
        agent_state = AgentState(base_commit=base, workspace=ws)
        result = scorer.score("", task, agent_state=agent_state)
        assert result.verdict == "correct"

        after = {
            p.name
            for p in tmp_root.iterdir()
            if p.name.startswith("codeprobe-score-")
        }
        leaked = after - before
        assert not leaked, (
            f"Materialisation leaked tempdirs in {tmp_root}: {leaked}"
        )


# ---------------------------------------------------------------------------
# Smoke test: imports + dataclass shape
# ---------------------------------------------------------------------------


class TestAgentStateShape:
    """Pin the public-ish shape of ``AgentState`` so callers can rely on it."""

    def test_is_frozen_dataclass(self) -> None:
        state = AgentState(base_commit="abc123", workspace=Path("/tmp"))
        with pytest.raises((AttributeError, Exception)):
            # Frozen dataclasses raise FrozenInstanceError on mutation.
            state.base_commit = "different"  # type: ignore[misc]

    def test_fields_are_accessible(self) -> None:
        state = AgentState(base_commit="abc123", workspace=Path("/tmp/ws"))
        assert state.base_commit == "abc123"
        assert state.workspace == Path("/tmp/ws")
