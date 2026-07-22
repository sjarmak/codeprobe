"""Resume semantics for mine_tasks + MineState (codeprobe-f7rl.14).

Covers the contract the ``mine --resume`` CLI flag relies on:

- commits recorded ``completed`` in a prior run are skipped (no git
  subprocesses re-run for them) and new commits still complete;
- an exception mid-extraction — including KeyboardInterrupt — records the
  in-flight SHA as ``interrupted``, never ``completed``, so a resume
  re-processes it;
- ``MineState.reset()`` clears every row (fresh, non-resume mines start
  from a clean slate).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from codeprobe.mining.extractor import mine_tasks
from codeprobe.mining.state import MineState
from codeprobe.paths import compute_repo_hash

TENANT = "acme"
REPO_HASH = compute_repo_hash("git@example.com:acme/repo.git", "main", "/tmp/wt")

SHA_DONE = "aaaa1111bbbb2222"
SHA_NEW = "cccc3333dddd4444"

_MERGE_LOG = f"{SHA_DONE} Merge PR #1 feature\n{SHA_NEW} Merge PR #2 bugfix\n"


def _extractor_side_effect(cmd, **kwargs):
    """Serve ``git log`` (merge list / commit body) and ``git diff`` calls."""
    if "log" in cmd:
        return subprocess.CompletedProcess(cmd, 0, _MERGE_LOG, "")
    return subprocess.CompletedProcess(cmd, 0, "src/bug.py\ntests/test_bug.py\n", "")


class TestResumeSkipsCompleted:
    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_completed_sha_is_skipped_and_new_sha_completes(
        self, mock_run, tenant_state_root: Path
    ) -> None:
        """A SHA recorded completed is never re-processed; new SHAs mine."""
        mock_run.side_effect = _extractor_side_effect

        with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH) as state:
            state.record_running(SHA_DONE)
            state.record_completed(SHA_DONE)

        with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH) as state:
            result = mine_tasks(
                Path("/fake/repo"),
                count=5,
                source_hint="local",
                min_quality=0.0,
                state=state,
            )
            completed = state.completed_shas()

        # Only the un-mined SHA yields a task.
        assert len(result.tasks) == 1
        # The completed SHA ran zero git subprocesses (allowlist skip).
        diff_targets = [
            c.args[0] for c in mock_run.call_args_list if "log" not in c.args[0]
        ]
        assert all(SHA_DONE not in " ".join(cmd) for cmd in diff_targets)
        # Both SHAs are now completed — a second resume would skip both.
        assert completed == {SHA_DONE, SHA_NEW}

    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_without_state_no_skip(self, mock_run, tenant_state_root: Path) -> None:
        """No state store: every merge is processed (baseline behavior)."""
        mock_run.side_effect = _extractor_side_effect

        result = mine_tasks(
            Path("/fake/repo"), count=5, source_hint="local", min_quality=0.0
        )
        assert len(result.tasks) == 2


class TestRelaxationRetryWithState:
    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_min_files_relaxation_still_yields_tasks_with_state(
        self, mock_run, tenant_state_root: Path
    ) -> None:
        """The relaxed retry re-examines SHAs the strict pass rejected.

        The strict pass records rejected SHAs as completed; the retry must
        skip only commits completed by a PRIOR invocation, or relaxation
        would silently become a no-op whenever state is threaded.
        """
        mock_run.side_effect = _extractor_side_effect

        with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH) as state:
            result = mine_tasks(
                Path("/fake/repo"),
                count=5,
                source_hint="local",
                min_files=5,  # both fixture PRs change only 2 files
                min_quality=0.0,
                state=state,
            )

        assert len(result.tasks) == 2
        assert result.min_files_used is not None
        assert result.min_files_used < 5


class TestInterruptRecordsInterrupted:
    @patch("codeprobe.mining.extractor.subprocess.run")
    def test_keyboard_interrupt_mid_extract_never_marks_completed(
        self, mock_run, tenant_state_root: Path
    ) -> None:
        """Ctrl-C during per-commit processing leaves the SHA re-mineable."""

        def _interrupting(cmd, **kwargs):
            if "log" in cmd:
                return subprocess.CompletedProcess(cmd, 0, _MERGE_LOG, "")
            raise KeyboardInterrupt

        mock_run.side_effect = _interrupting

        with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH) as state:
            with pytest.raises(KeyboardInterrupt):
                mine_tasks(
                    Path("/fake/repo"),
                    count=5,
                    source_hint="local",
                    min_quality=0.0,
                    state=state,
                )

        # Reopen exactly like a resume would: the in-flight SHA must be
        # interrupted (re-processed next run), never completed.
        with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH) as state:
            assert state.status(SHA_DONE) == "interrupted"
            assert state.completed_shas() == set()


class TestReset:
    def test_reset_clears_all_rows(self, tenant_state_root: Path) -> None:
        """reset() removes every row so fresh mines never inherit skips."""
        with MineState.open(tenant_id=TENANT, repo_hash=REPO_HASH) as state:
            state.record_running(SHA_DONE)
            state.record_completed(SHA_DONE)
            state.record_interrupted(SHA_NEW, error="boom")
            assert state.reset() == 2
            assert state.all_rows() == []
            assert state.completed_shas() == set()
